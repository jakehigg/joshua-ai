"""How a snapshot row reaches the agent, and where it never goes.

- The per-turn injection shows a snapshot chunk as one short line under its
  own header, and the chunk takes a slot of `top_k`.
- `search_memory` points a snapshot result at the live tool, and shortens
  every result to a snippet unless the call asks for `full`.
- `POST /v1/memory/search` never returns a snapshot row.

The stores are fakes that serve canned chunks. The embedding is a constant
vector.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from joshua_core.engine import injection
from joshua_core.engine.injection import SNAPSHOT_HEADER, kb_context
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.recall import SNIPPET_CHARS, do_search_memory
from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk
from joshua_core.search_api import SEARCH_PATH, build_search_router
from joshua_core.store.models import Channel, Conversation
from joshua_shared.config import Memory, MemorySearch, Snapshot
from joshua_shared.contracts import SEARCH_KINDS
from starlette.testclient import TestClient

TZ = "America/New_York"
FETCHED = datetime(2026, 3, 14, 15, 0, tzinfo=UTC)
HEAD = "open-tasks as of 2026-03-14 11:00 EDT"


def _snapshot(sim: float, item: str = "title: Change the filter", n: int = 1) -> KbChunk:
    return KbChunk(
        source="snapshots",
        kind="snapshot",
        path=f"snapshot/open-tasks/{n:012d}",
        title="open-tasks",
        text=f"open-tasks:\n{item}",
        similarity=sim,
    )


def _wiki(
    sim: float, path: str = "wiki/house/filter.md", text: str = "Filter size 16x25."
) -> KbChunk:
    return KbChunk(
        source="files", kind="wiki", path=path, title="Filter", text=text, similarity=sim
    )


class Store:
    def __init__(self, rows: list[KbChunk], times: dict | None = None) -> None:
        self.rows = rows
        self.times = {"open-tasks": FETCHED} if times is None else times
        self.time_calls: list[list[str]] = []

    async def snapshot_times(self, names):
        self.time_calls.append(list(names))
        return {n: t for n, t in self.times.items() if n in names}

    async def kb_search(
        self, person_id, embedding, *, k, min_sim, sources=None, kinds=None, exclude_kinds=None
    ):
        return list(self.rows)


class Repo:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def add_kb_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


@pytest.fixture(autouse=True)
def _const_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [0.1] * 384)


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        text="I changed the filter today",
        person_id="alex",
        conversation=SimpleNamespace(id="c1"),
        turn_id="t1",
    )


async def _inject(
    rows: list[KbChunk], top_k: int = 2, store: Store | None = None
) -> tuple[str | None, Repo]:
    repo = Repo()
    note = await kb_context(
        _ctx(),
        store=store or Store(rows),
        repo=repo,
        timezone=TZ,
        embed_model="m",
        full_sim=0.72,
        hint_sim=0.60,
        top_k=top_k,
        max_chars=1200,
        recency_bonus=0.0,
        recency_half_life_days=14.0,
        per_doc_cap=3,
    )
    return note, repo


# --- injection ---------------------------------------------------------------


async def test_a_snapshot_is_one_short_line_under_its_header() -> None:
    note, repo = await _inject([_snapshot(0.95)])
    assert note == f"{SNAPSHOT_HEADER}\n{HEAD}: title: Change the filter"
    assert "read_file" not in note
    assert repo.events[0]["decision"] == "snapshot"
    assert repo.events[0]["results"][0]["injected"] is True


async def test_the_header_says_to_check_the_live_tool() -> None:
    assert "live" in SNAPSHOT_HEADER
    assert "check before you change anything" in SNAPSHOT_HEADER


async def test_a_long_item_is_cut_to_200_characters_on_one_line() -> None:
    note, _ = await _inject([_snapshot(0.9, item="word\n" * 100)])
    assert note is not None
    line = note.splitlines()[1]
    assert line.startswith(f"{HEAD}: word word")
    assert len(line) == len(HEAD) + 2 + 200


async def test_a_snapshot_takes_a_slot_of_top_k() -> None:
    rows = [_snapshot(0.95), _wiki(0.9, "wiki/a.md", "Alpha"), _wiki(0.85, "wiki/b.md", "Beta")]
    note, _ = await _inject(rows, top_k=2)
    assert note is not None
    assert "Alpha" in note
    assert "Beta" not in note
    assert note.endswith(f"{SNAPSHOT_HEADER}\n{HEAD}: title: Change the filter")


async def test_the_wiki_chunks_keep_their_decision() -> None:
    hint, _ = await _inject([_wiki(0.65), _snapshot(0.64)])
    assert hint is not None
    assert hint.startswith("[Related notes and files may help")
    assert SNAPSHOT_HEADER in hint
    full, repo = await _inject([_wiki(0.9), _snapshot(0.8)])
    assert full is not None and full.startswith("[From your notes and files")
    assert repo.events[0]["decision"] == "full"


async def test_snapshots_in_every_slot_leave_the_wiki_a_hint() -> None:
    note, _ = await _inject([_snapshot(0.95, n=1), _snapshot(0.94, "B", n=2), _wiki(0.9)])
    assert note is not None
    assert note.startswith("[Related notes and files may help")


async def test_a_snapshot_below_the_top_k_is_left_out() -> None:
    note, _ = await _inject([_wiki(0.9, "wiki/a.md"), _wiki(0.88, "wiki/b.md"), _snapshot(0.7)])
    assert note is not None
    assert SNAPSHOT_HEADER not in note


async def test_a_snapshot_with_no_good_fetch_is_dropped() -> None:
    rows = [_snapshot(0.95), _wiki(0.9)]
    note, _ = await _inject(rows, store=Store(rows, times={}))
    assert note is not None
    assert SNAPSHOT_HEADER not in note
    assert "Filter size" in note


async def test_the_times_are_read_once_for_the_turn() -> None:
    rows = [_snapshot(0.95, n=1), _snapshot(0.94, "B", n=2)]
    store = Store(rows)
    await _inject(rows, store=store)
    assert store.time_calls == [["open-tasks"]]


async def test_an_unchanged_item_shows_the_latest_fetch_time() -> None:
    rows = [_snapshot(0.95)]
    later = {"open-tasks": FETCHED + timedelta(days=7)}
    note, _ = await _inject(rows, store=Store(rows, times=later))
    line = "open-tasks as of 2026-03-21 11:00 EDT: title: Change the filter"
    assert note == f"{SNAPSHOT_HEADER}\n{line}"


def test_split_snapshots() -> None:
    ranked = [_snapshot(0.9), _wiki(0.8), _snapshot(0.7, n=2)]
    snaps, others = injection.split_snapshots(ranked, 2)
    assert [c.path for c in snaps] == [ranked[0].path]
    assert others == [ranked[1]]


# --- search_memory -----------------------------------------------------------


def _deps() -> ToolDeps:
    conv = Conversation(id="c1", channel_id="telegram:1", person_id="alex")
    return ToolDeps(
        repo=None,
        conversation=conv,
        channel=Channel(id="telegram:1", channel_type="telegram"),
        tz=TZ,
        person_id="alex",
    )


SNAPS = [Snapshot(name="open-tasks", server="tasks", tool="list_items", every="5m")]


async def _search(
    rows: list[KbChunk], store: Store | None = None, **kw: Any
) -> list[dict[str, Any]]:
    return await do_search_memory(
        _deps(),
        store=store or Store(rows),
        embed_model="m",
        min_sim=0.5,
        query="filter",
        snapshots=SNAPS,
        **kw,
    )


async def test_a_snapshot_result_points_at_the_live_tool() -> None:
    (result,) = await _search([_snapshot(0.9)])
    assert "path" not in result
    assert result["snapshot"] == f"snapshot {HEAD}; for live data call tasks list_items"
    assert result["title"] == "open-tasks"


async def test_search_drops_a_snapshot_with_no_good_fetch() -> None:
    rows = [_snapshot(0.9), _wiki(0.8)]
    results = await _search(rows, store=Store(rows, times={}))
    assert [r.get("path") for r in results] == ["wiki/house/filter.md"]


async def test_a_result_is_a_snippet_by_default() -> None:
    long = "x" * (SNIPPET_CHARS + 50)
    (result,) = await _search([_wiki(0.9, text=long)])
    assert result["text"] == "x" * SNIPPET_CHARS
    assert result["cut"] is True
    assert result["path"] == "wiki/house/filter.md"
    assert result["title"] == "Filter"


async def test_full_returns_the_whole_chunk() -> None:
    long = "x" * (SNIPPET_CHARS + 50)
    (result,) = await _search([_wiki(0.9, text=long)], full=True)
    assert result["text"] == long
    assert "cut" not in result


async def test_a_short_result_is_not_marked_cut() -> None:
    (result,) = await _search([_wiki(0.9)])
    assert result["text"] == "Filter size 16x25."
    assert "cut" not in result


# --- the search route --------------------------------------------------------


def test_snapshot_is_not_a_search_kind() -> None:
    assert "snapshot" not in SEARCH_KINDS


def test_the_search_route_never_returns_a_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", "viewer-token")
    rows = [_snapshot(0.99), {**_snapshot(0.98).__dict__}, _wiki(0.9)]
    # The second row claims the files source, so only the kind can stop it.
    rows[1] = KbChunk(**{**rows[1], "source": "files"})
    settings = SimpleNamespace(memory=Memory(search=MemorySearch(allowed_callers=["viewer"])))
    app = FastAPI()
    app.state.ctx = SimpleNamespace(settings=settings, memory=Store(rows))
    app.include_router(build_search_router())
    response = TestClient(app).post(
        SEARCH_PATH, json={"query": "filter"}, headers={"Authorization": "Bearer viewer-token"}
    )
    assert response.status_code == 200
    assert [hit["path"] for hit in response.json()["results"]] == ["wiki/house/filter.md"]
