"""The ``snapshots`` source: the latest output of each configured snapshot.

A snapshot is a fixed MCP tool call that core runs on a cadence
(``joshua_core.snapshots``). The runner keeps the latest output in the
``snapshot_result`` table, and this source offers it to the indexer as one
document per item:

- The text is read as JSON. With ``items``, the item list is at that dotted
  path. A JSON list is the item list. Anything else is one item, the whole
  text.
- A JSON object item becomes short ``key: value`` lines with no empty value. A
  string item is used as it is.
- The first line of each document is ``<name>:``. The text holds no time,
  so an item that stays the same keeps its uri, its revision, and its
  embedding from one fetch to the next. The time is added when a chunk is
  shown: ``label`` makes ``<name> as of <local time of the latest good
  fetch>`` from ``fetched_at`` (``MemoryStore.snapshot_times``).
- The uri is ``snapshot/<name>/<12 hex of the sha256 of the item text>``, so an
  unchanged item is not embedded again, and the indexer purges the rows of an
  item that is gone. The kind of the rows is ``snapshot``.

A snapshot whose output is older than its ``max_age``, or that failed three
times or more in a row, lists no document, so its rows leave the index. The
index never holds stale state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from joshua_shared.config import Snapshot
from joshua_shared.log import get_logger

from joshua_core.memory.models import KbChunk
from joshua_core.memory.sources import Document
from joshua_core.memory.store import SNAPSHOT_KIND

logger = get_logger("memory.sources.snapshots")

SOURCE_NAME = "snapshots"
URI_PREFIX = "snapshot/"
# A snapshot that failed this many times in a row lists nothing.
MAX_FAILURES = 3
# How the fetch time shows in the first line of a document.
TIME_FORMAT = "%Y-%m-%d %H:%M %Z"


class SnapshotTimes(Protocol):
    """What a caller that shows snapshot chunks reads: ``MemoryStore``."""

    async def snapshot_times(self, names: Sequence[str]) -> dict[str, datetime]: ...


async def snapshot_labels(
    store: SnapshotTimes, chunks: Sequence[KbChunk], timezone: str
) -> dict[str, str]:
    """``{name: "<name> as of <local time>"}`` for each snapshot in ``chunks``
    that has a good fetch, from one query.

    A caller drops a snapshot chunk whose name is not in the answer. A failed
    query gives ``{}``, so no snapshot chunk is shown with a wrong time.
    """
    names = sorted({c.title for c in chunks if c.kind == SNAPSHOT_KIND})
    if not names:
        return {}
    try:
        times = await store.snapshot_times(names)
    except Exception as exc:  # noqa: BLE001 — a missing time drops the chunk
        logger.warning({"message": "snapshot times unavailable", "error": str(exc)})
        return {}
    return {name: label(name, at, timezone) for name, at in times.items() if name in names}


def shown(chunks: Sequence[KbChunk], labels: dict[str, str]) -> list[KbChunk]:
    """``chunks`` without the snapshot chunks that have no label."""
    return [c for c in chunks if c.kind != SNAPSHOT_KIND or c.title in labels]


class SnapshotRows(Protocol):
    """What the source reads: the ``snapshot_result`` rows."""

    async def list_snapshot_results(self) -> list[dict[str, Any]]: ...


def _dig(value: Any, path: str) -> Any:
    """The value at the dotted ``path`` in ``value``, or None."""
    for key in path.split("."):
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            return None
    return value


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _scalar(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def render_item(item: Any, fields: Sequence[str] | None = None) -> str:
    """The text of one item: ``key: value`` lines for an object, the string
    itself for a string, else compact JSON. With ``fields``, an object keeps
    only those keys, in that order."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        pairs = [(key, item.get(key)) for key in fields] if fields else list(item.items())
        return "\n".join(f"{key}: {_scalar(value)}" for key, value in pairs if not _is_empty(value))
    return _scalar(item)


def _json_values(text: str) -> list[Any] | None:
    """The JSON values of ``text``: one document, or a run of documents one
    after the other, as an MCP server gives a list in one text part per
    element. A last value that is cut off is left out. None when the text does
    not start with a JSON value."""
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    values: list[Any] = []
    index = 0
    end = len(text)
    while True:
        while index < end and text[index].isspace():
            index += 1
        if index >= end:
            break
        try:
            value, index = decoder.raw_decode(text, index)
        except ValueError:
            break
        values.append(value)
    return values or None


def split_items(
    text: str, items_path: str | None, fields: Sequence[str] | None = None
) -> list[str]:
    """The item texts of one snapshot output. An empty item is left out."""
    values = _json_values(text)
    if values is None:
        stripped = text.strip()
        return [stripped] if stripped else []
    raw: list[Any] = []
    for parsed in values:
        if items_path:
            found = _dig(parsed, items_path)
            if found is not None:
                parsed = found
        raw.extend(parsed if isinstance(parsed, list) else [parsed])
    rendered = [render_item(item, fields) for item in raw]
    return [item for item in rendered if item]


def item_hash(item_text: str) -> str:
    return hashlib.sha256(item_text.encode("utf-8")).hexdigest()


def item_uri(name: str, item_text: str) -> str:
    """``snapshot/<name>/<12 hex>``: stable while the item text is the same."""
    return f"{URI_PREFIX}{name}/{item_hash(item_text)[:12]}"


def is_listed(row: dict[str, Any] | None, snap: Snapshot, now: datetime) -> bool:
    """True when the row of ``snap`` may be in the index at ``now``: it has a
    good output, younger than ``max_age``, and fewer than three failures."""
    if row is None or row.get("text") is None or row.get("fetched_at") is None:
        return False
    if int(row.get("failures") or 0) >= MAX_FAILURES:
        return False
    return now - row["fetched_at"] <= timedelta(seconds=snap.max_age_seconds)


def as_of(fetched_at: datetime, timezone: str) -> str:
    """The fetch time in the instance timezone, as the documents show it."""
    return fetched_at.astimezone(ZoneInfo(timezone)).strftime(TIME_FORMAT)


def label(name: str, fetched_at: datetime, timezone: str) -> str:
    """``<name> as of <local time>``: how a shown snapshot item names its
    snapshot and the time of the latest good fetch."""
    return f"{name} as of {as_of(fetched_at, timezone)}"


def item_body(text: str) -> str:
    """The item text of a snapshot chunk, without its ``<name>:`` line."""
    head, sep, body = text.partition("\n")
    if sep and head.endswith(":") and " " not in head:
        return body
    return text


def documents_for(snap: Snapshot, row: dict[str, Any], timezone: str) -> list[Document]:
    """One document per item of the output in ``row``."""
    fetched_at: datetime = row["fetched_at"]
    local = fetched_at.astimezone(ZoneInfo(timezone))
    header = f"{snap.name}:"
    docs: dict[str, Document] = {}
    for item_text in split_items(row["text"] or "", snap.items, snap.fields):
        uri = item_uri(snap.name, item_text)
        docs[uri] = Document(
            source=SOURCE_NAME,
            uri=uri,
            person_id=snap.person_id,
            title=snap.name,
            text=f"{header}\n{item_text}",
            updated_at=fetched_at,
            rev=item_hash(item_text),
            provenance="external",
            doc_date=local.date(),
        )
    return list(docs.values())


class SnapshotsSource:
    """List the items of each fresh snapshot from ``snapshot_result``."""

    name = SOURCE_NAME

    def __init__(
        self,
        rows: SnapshotRows,
        snapshots: Sequence[Snapshot],
        *,
        timezone: str,
        clock: Callable[[], datetime] | None = None,
    ):
        self._rows = rows
        self._snapshots = list(snapshots)
        self._timezone = timezone
        self._clock = clock or (lambda: datetime.now(UTC))

    async def _documents(self) -> list[Document]:
        rows = {row["name"]: row for row in await self._rows.list_snapshot_results()}
        now = self._clock()
        out: list[Document] = []
        for snap in self._snapshots:
            row = rows.get(snap.name)
            if row is None or not is_listed(row, snap, now):
                continue
            out.extend(documents_for(snap, row, self._timezone))
        return out

    async def list_documents(self) -> AsyncIterator[Document]:
        for doc in await self._documents():
            yield doc

    async def fetch(self, uri: str) -> Document | None:
        for doc in await self._documents():
            if doc.uri == uri:
                return doc
        return None
