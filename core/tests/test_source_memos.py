"""Offline tests for the ``memos`` source adapter over a fake Memos server."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest
from joshua_core.memory.sources.memos import (
    MemosSource,
    build_memos_source,
    schedule_seconds,
)

# Three memos plus control cases. Keys mirror the Memos REST v1 memo resource.
_MEMOS: list[dict[str, Any]] = [
    {
        "name": "memos/protected",
        "state": "NORMAL",
        "visibility": "PROTECTED",
        "content": "# Grocery run\n\nMilk, eggs, and bread for the week.",
        "createTime": "2026-08-20T10:00:00Z",
        "updateTime": "2026-08-21T09:00:00Z",
        "tags": ["home"],
    },
    {
        "name": "memos/public",
        "state": "NORMAL",
        "visibility": "PUBLIC",
        "content": "Public note about the shared garden project.",
        "createTime": "2026-08-22T10:00:00Z",
        "updateTime": "2026-08-22T10:00:00Z",
        "tags": [],
    },
    {
        "name": "memos/private",
        "state": "NORMAL",
        "visibility": "PRIVATE",
        "content": "Private thought that must never be indexed.",
        "createTime": "2026-08-23T10:00:00Z",
        "updateTime": "2026-08-23T10:00:00Z",
        "tags": [],
    },
    {
        "name": "memos/alex",
        "state": "NORMAL",
        "visibility": "PROTECTED",
        "content": "Alex started a new job this week.",
        "createTime": "2026-08-24T10:00:00Z",
        "updateTime": "2026-08-24T10:00:00Z",
        "tags": ["person/alex", "work"],
    },
    {
        "name": "memos/old",
        "state": "NORMAL",
        "visibility": "PROTECTED",
        "content": "This note is deprecated and should be purged.",
        "createTime": "2026-08-19T10:00:00Z",
        "updateTime": "2026-08-19T10:00:00Z",
        "tags": ["deprecated"],
    },
    {
        "name": "memos/archived",
        "state": "ARCHIVED",
        "visibility": "PROTECTED",
        "content": "Archived note, not indexed.",
        "createTime": "2026-08-18T10:00:00Z",
        "updateTime": "2026-08-18T10:00:00Z",
        "tags": [],
    },
]


class _FakeMemos:
    """A minimal Memos REST v1 server as an httpx transport."""

    def __init__(self, memos: list[dict[str, Any]], *, page_size: int | None = None) -> None:
        self._memos = memos
        self._page_size = page_size
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/api/v1/memos":
            return self._list(request)
        name = path.removeprefix("/api/v1/")
        memo = next((m for m in self._memos if m["name"] == name), None)
        if memo is None:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json=memo)

    def _list(self, request: httpx.Request) -> httpx.Response:
        start = int(request.url.params.get("pageToken") or 0)
        size = self._page_size or len(self._memos)
        window = self._memos[start : start + size]
        next_token = str(start + size) if start + size < len(self._memos) else ""
        return httpx.Response(200, json={"memos": window, "nextPageToken": next_token})


def _source(fake: _FakeMemos, **kwargs: Any) -> MemosSource:
    return MemosSource(url="https://memos.test", token="t", transport=fake.transport(), **kwargs)


async def test_visibility_filter_and_scope() -> None:
    fake = _FakeMemos(_MEMOS)
    src = _source(fake)
    docs = {d.uri: d async for d in src.list_documents()}

    # PROTECTED and PUBLIC land under shared; PRIVATE is absent.
    assert "memos/protected" in docs
    assert "memos/public" in docs
    assert "memos/private" not in docs
    assert docs["memos/protected"].person_id is None
    assert docs["memos/public"].person_id is None
    # Deprecated and archived memos are never yielded.
    assert "memos/old" not in docs
    assert "memos/archived" not in docs
    # The server request carries a visibility filter, not PRIVATE.
    list_req = next(r for r in fake.requests if r.url.path == "/api/v1/memos")
    assert 'visibility in ["PROTECTED", "PUBLIC"]' == list_req.url.params.get("filter")


async def test_person_tag_routes_scope() -> None:
    fake = _FakeMemos(_MEMOS)
    docs = {d.uri: d async for d in _source(fake).list_documents()}
    alex = docs["memos/alex"]
    assert alex.person_id == "alex"  # person/alex tag overrides shared scope
    assert "work" in alex.tags


async def test_document_fields() -> None:
    fake = _FakeMemos(_MEMOS)
    docs = {d.uri: d async for d in _source(fake).list_documents()}
    doc = docs["memos/protected"]
    assert doc.source == "memos"
    assert doc.title == "Grocery run"  # first line, heading marker stripped
    assert doc.provenance == "external"
    assert doc.rev == "2026-08-21T09:00:00Z"  # updateTime is the change marker
    assert doc.doc_date == date(2026, 8, 20)  # createTime


async def test_unchanged_update_time_keeps_rev_stable() -> None:
    fake = _FakeMemos(_MEMOS)
    src = _source(fake)
    first = {d.uri: d.rev async for d in src.list_documents()}
    second = {d.uri: d.rev async for d in src.list_documents()}
    # A re-run with unchanged updateTime yields the same rev, so the indexer
    # re-embeds nothing.
    assert first == second


async def test_paging_follows_next_token() -> None:
    fake = _FakeMemos(_MEMOS, page_size=2)
    uris = [d.uri async for d in _source(fake).list_documents()]
    assert set(uris) == {"memos/protected", "memos/public", "memos/alex"}
    # More than one page was fetched.
    assert sum(1 for r in fake.requests if r.url.path == "/api/v1/memos") >= 2


async def test_scope_person_places_shared_under_person() -> None:
    fake = _FakeMemos(_MEMOS)
    src = _source(fake, scope="person:alice")
    docs = {d.uri: d async for d in src.list_documents()}
    assert docs["memos/protected"].person_id == "alice"
    # A person tag still overrides the configured scope.
    assert docs["memos/alex"].person_id == "alex"


async def test_visibility_can_include_private() -> None:
    fake = _FakeMemos(_MEMOS)
    src = _source(fake, visibility=["PRIVATE"])
    docs = {d.uri async for d in src.list_documents()}
    assert docs == {"memos/private"}


async def test_fetch_resolves_and_missing_is_none() -> None:
    fake = _FakeMemos(_MEMOS)
    src = _source(fake)
    doc = await src.fetch("memos/public")
    assert doc is not None and doc.person_id is None
    # A hidden memo is treated as gone so its rows get purged.
    assert await src.fetch("memos/private") is None
    assert await src.fetch("memos/missing") is None


async def test_build_from_config() -> None:
    fake = _FakeMemos(_MEMOS)
    options = {
        "url": "https://memos.test",
        "token": "t",
        "scope": "shared",
        "visibility": ["PROTECTED", "PUBLIC"],
    }
    src = build_memos_source(options, transport=fake.transport())
    uris = {d.uri async for d in src.list_documents()}
    assert "memos/protected" in uris and "memos/private" not in uris


def test_build_requires_url() -> None:
    with pytest.raises(ValueError):
        build_memos_source({"token": "t"})


def test_schedule_seconds() -> None:
    assert schedule_seconds(None) == 86400.0
    assert schedule_seconds("nightly") == 86400.0
    assert schedule_seconds("daily") == 86400.0
    assert schedule_seconds("every 6h") == 6 * 3600
    assert schedule_seconds("6h") == 6 * 3600
    assert schedule_seconds("30m") == 1800
    assert schedule_seconds("90s") == 90
    assert schedule_seconds(120) == 120.0
    assert schedule_seconds("300") == 300.0
    with pytest.raises(ValueError):
        schedule_seconds("sometimes")


def test_json_payload_is_valid() -> None:
    # Guards the fixture: every memo serializes (the transport returns JSON).
    assert json.loads(json.dumps(_MEMOS))
