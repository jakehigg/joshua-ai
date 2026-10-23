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
- The first line of each document is ``<name> as of <local time>``.
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

from joshua_core.memory.sources import Document

SOURCE_NAME = "snapshots"
URI_PREFIX = "snapshot/"
# A snapshot that failed this many times in a row lists nothing.
MAX_FAILURES = 3
# How the fetch time shows in the first line of a document.
TIME_FORMAT = "%Y-%m-%d %H:%M %Z"


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


def render_item(item: Any) -> str:
    """The text of one item: ``key: value`` lines for an object, the string
    itself for a string, else compact JSON."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return "\n".join(
            f"{key}: {_scalar(value)}" for key, value in item.items() if not _is_empty(value)
        )
    return _scalar(item)


def split_items(text: str, items_path: str | None) -> list[str]:
    """The item texts of one snapshot output. An empty item is left out."""
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        stripped = text.strip()
        return [stripped] if stripped else []
    if items_path:
        found = _dig(parsed, items_path)
        if found is not None:
            parsed = found
    raw = parsed if isinstance(parsed, list) else [parsed]
    rendered = [render_item(item) for item in raw]
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


def documents_for(snap: Snapshot, row: dict[str, Any], timezone: str) -> list[Document]:
    """One document per item of the output in ``row``."""
    fetched_at: datetime = row["fetched_at"]
    local = fetched_at.astimezone(ZoneInfo(timezone))
    header = f"{snap.name} as of {local.strftime(TIME_FORMAT)}"
    docs: dict[str, Document] = {}
    for item_text in split_items(row["text"] or "", snap.items):
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
