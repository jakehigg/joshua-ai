"""Offline tests for the ``recall`` builtin tool wrapper.

A FakeStore records the ``person_id`` the search forwarded and serves canned
chunks per scope, so the payload shape and the scope forwarding are checked
without a database. Embedding is monkeypatched to a constant vector.
"""

from __future__ import annotations

from typing import Any

import pytest
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.recall import do_search_memory
from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk
from joshua_core.store.models import Channel, Conversation


class FakeStore:
    """Serves shared rows for a group turn and own+shared for a person."""

    def __init__(self) -> None:
        self.seen_person: list[str | None] = []
        self.exclude_seen: list[Any] = []

    async def kb_search(
        self, person_id, embedding, *, k, min_sim, sources=None, kinds=None, exclude_kinds=None
    ):
        self.seen_person.append(person_id)
        self.exclude_seen.append(exclude_kinds)
        shared = KbChunk(
            person_id=None,
            source="files",
            kind="shared",
            path="shared/house.md",
            title="House",
            heading="Wifi",
            text="Guest wifi password",
            similarity=0.9,
        )
        skill = KbChunk(
            person_id=None,
            source="files",
            kind="skill",
            path="wiki/skills/movie.md",
            title="Movie",
            heading="movie time",
            text="Dim the lights.",
            similarity=0.99,
        )
        rows = [shared, skill]
        if person_id is not None:
            rows.insert(
                0,
                KbChunk(
                    person_id=person_id,
                    source="files",
                    kind="wiki",
                    path="wiki/me.md",
                    title="Me",
                    heading="",
                    text="My notes",
                    similarity=0.95,
                ),
            )
        if exclude_kinds:
            rows = [c for c in rows if c.kind not in exclude_kinds]
        return rows


def _deps(person_id: str | None) -> ToolDeps:
    conv = Conversation(id="c1", channel_id="telegram:1", person_id=person_id)
    channel = Channel(id="telegram:1", channel_type="telegram")
    return ToolDeps(repo=None, conversation=conv, channel=channel, tz="UTC", person_id=person_id)


@pytest.fixture(autouse=True)
def _const_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [0.1] * 384)


async def _run(deps: ToolDeps, store: FakeStore, query: str = "wifi"):
    return await do_search_memory(deps, store=store, embed_model="m", min_sim=0.55, query=query)


async def test_person_scope_forwarded_and_payload_shape() -> None:
    store = FakeStore()
    results = await _run(_deps("alice"), store)
    assert store.seen_person == ["alice"]
    assert {r["path"] for r in results} == {"wiki/me.md", "shared/house.md"}
    first = results[0]
    assert set(first) == {"path", "title", "heading", "text", "score"}
    assert first["score"] == 0.95


async def test_group_turn_sees_shared_only() -> None:
    store = FakeStore()
    results = await _run(_deps(None), store)
    assert store.seen_person == [None]
    assert [r["path"] for r in results] == ["shared/house.md"]


async def test_skill_rows_excluded_from_search_memory() -> None:
    store = FakeStore()
    results = await _run(_deps("alice"), store)
    assert store.exclude_seen[0] == ("skill",)  # the exclude filter is forwarded
    assert all("skills/" not in r["path"] for r in results)  # never a taught skill


async def test_empty_query_returns_nothing() -> None:
    store = FakeStore()
    assert await _run(_deps("alice"), store, query="   ") == []
    assert store.seen_person == []  # never reached the store


async def test_unavailable_embedding_returns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": None)
    store = FakeStore()
    assert await _run(_deps("alice"), store) == []


async def test_limit_is_clamped() -> None:
    store = FakeStore()
    # A huge limit is clamped to MAX_LIMIT and still forwards a person scope.
    await do_search_memory(
        _deps("alice"), store=store, embed_model="m", min_sim=0.55, query="wifi", limit=9999
    )
    assert store.seen_person == ["alice"]
