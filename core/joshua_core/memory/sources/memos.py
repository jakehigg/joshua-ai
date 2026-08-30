"""The ``memos`` source adapter — read-only scrape of a Memos instance.

Reads notes from a `Memos <https://usememos.com>`_ server over its REST v1 API
and offers each one to the indexer as a :class:`Document`. The adapter never
writes to Memos; the token needs read access only. Give the Memos user no more
visibility than you want indexed.

``list_documents`` pages ``GET /api/v1/memos`` and filters on visibility;
``fetch`` resolves one memo by its ``memos/<id>`` name. A memo is skipped when
its state is not ``NORMAL``, when its visibility is outside the configured set,
or when it carries the ``deprecated`` tag — a skipped memo is not yielded, so the
indexer purges its rows on the next pass. ``rev`` is the memo ``updateTime``, so
an unchanged memo re-embeds nothing.

Scope: a memo lands under the configured ``scope`` (``shared`` → shared, or
``person:<id>``), unless a tag with ``person_tag_prefix`` (default ``person/``)
routes it to that person.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
from typing import Any

import httpx
from joshua_shared import layout

from joshua_core.memory.sources import Document

_DEFAULT_VISIBILITY = ("PROTECTED", "PUBLIC")
_DEFAULT_PERSON_TAG_PREFIX = "person/"
_DEFAULT_PAGE_SIZE = 200
_DEPRECATED_TAG = "deprecated"

_SCHEDULE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_SCHEDULE_RE = re.compile(r"(\d+)\s*([smhd])")
_DAY_SECONDS = 86400.0


def schedule_seconds(value: Any) -> float:
    """Turn a ``schedule`` option into a cadence in seconds for the index loop.

    ``nightly``/``daily``/empty → one day. A plain number is seconds. ``every 6h``
    or ``6h`` (``s``/``m``/``h``/``d``) is that interval.
    """
    if value in (None, "", "nightly", "daily"):
        return _DAY_SECONDS
    if isinstance(value, bool):
        raise ValueError(f"invalid memos schedule {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if text.startswith("every"):
        text = text[len("every") :].strip()
    if text.isdigit():
        return float(text)
    m = _SCHEDULE_RE.fullmatch(text)
    if m:
        return float(int(m.group(1)) * _SCHEDULE_UNITS[m.group(2)])
    raise ValueError(f"invalid memos schedule {value!r}")


def _scope_person(scope: str) -> str | None:
    """The person a ``scope`` option maps to: ``shared`` → None (shared scope),
    ``person:<id>`` → that person id."""
    scope = (scope or "shared").strip()
    if scope == "shared":
        return None
    if scope.startswith("person:"):
        return layout.safe_segment(scope.split(":", 1)[1].strip())
    raise ValueError(f"memos scope must be 'shared' or 'person:<id>', got {scope!r}")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _first_line(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped
    return ""


class MemosSource:
    """Indexes memos from one Memos server. Optional; enabled in config."""

    name = "memos"

    def __init__(
        self,
        *,
        url: str,
        token: str = "",
        scope: str = "shared",
        visibility: Iterable[str] = _DEFAULT_VISIBILITY,
        person_tag_prefix: str = _DEFAULT_PERSON_TAG_PREFIX,
        page_size: int = _DEFAULT_PAGE_SIZE,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = url.rstrip("/")
        self._token = token
        self._scope_person = _scope_person(scope)
        self._visibility = tuple(v.strip().upper() for v in visibility)
        self._person_tag_prefix = person_tag_prefix
        self._page_size = page_size
        self._timeout = timeout
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=self._timeout,
            transport=self._transport,
        )

    def _filter(self) -> str:
        joined = ", ".join(f'"{v}"' for v in self._visibility)
        return f"visibility in [{joined}]"

    async def list_documents(self) -> AsyncIterator[Document]:
        async with self._client() as client:
            page_token = ""
            while True:
                params: dict[str, Any] = {"pageSize": self._page_size, "filter": self._filter()}
                if page_token:
                    params["pageToken"] = page_token
                response = await client.get("/api/v1/memos", params=params)
                response.raise_for_status()
                data = response.json()
                for memo in data.get("memos", []):
                    doc = self._to_document(memo)
                    if doc is not None:
                        yield doc
                page_token = data.get("nextPageToken") or ""
                if not page_token:
                    break

    async def fetch(self, uri: str) -> Document | None:
        """Resolve one memo by its ``memos/<id>`` name. A gone or now-hidden memo
        returns None so the indexer purges its rows."""
        async with self._client() as client:
            response = await client.get(f"/api/v1/{uri}")
            if response.status_code == httpx.codes.NOT_FOUND:
                return None
            response.raise_for_status()
            return self._to_document(response.json())

    def _person_for(self, tags: tuple[str, ...]) -> str | None:
        if self._person_tag_prefix:
            for tag in tags:
                if tag.startswith(self._person_tag_prefix):
                    candidate = tag[len(self._person_tag_prefix) :].strip()
                    try:
                        return layout.safe_segment(candidate)
                    except ValueError:
                        continue
        return self._scope_person

    def _to_document(self, memo: dict[str, Any]) -> Document | None:
        name = memo.get("name")
        if not name:
            return None
        if memo.get("state", "NORMAL") != "NORMAL":
            return None
        if memo.get("visibility", "").upper() not in self._visibility:
            return None
        tags = tuple(memo.get("tags") or ())
        if _DEPRECATED_TAG in tags:
            return None
        content = memo.get("content") or ""
        update_time = memo.get("updateTime") or ""
        doc_date = _parse_dt(memo.get("displayTime") or memo.get("createTime") or update_time)
        return Document(
            source=self.name,
            uri=name,
            person_id=self._person_for(tags),
            title=_first_line(content) or name,
            text=content,
            updated_at=_parse_dt(update_time) or datetime.now(UTC),
            rev=update_time,
            provenance="external",
            tags=tags,
            doc_date=doc_date.date() if isinstance(doc_date, datetime) else None,
        )


def build_memos_source(
    options: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
) -> MemosSource:
    """Build a :class:`MemosSource` from a ``memory.sources.memos`` config block."""
    url = options.get("url")
    if not url:
        raise ValueError("memos source requires 'url'")
    visibility = options.get("visibility") or list(_DEFAULT_VISIBILITY)
    if isinstance(visibility, str):
        visibility = [visibility]
    return MemosSource(
        url=str(url),
        token=str(options.get("token") or ""),
        scope=str(options.get("scope", "shared")),
        visibility=visibility,
        person_tag_prefix=str(options.get("person_tag_prefix", _DEFAULT_PERSON_TAG_PREFIX)),
        page_size=int(options.get("page_size", _DEFAULT_PAGE_SIZE)),
        transport=transport,
    )
