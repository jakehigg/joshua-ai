"""A closed or snoozed insight, and a question that is not open, never reach a
chat turn through retrieval: not the per-turn injection, not `search_memory`.
The rule reads the pages from the disk at query time, for a fixed day."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from insights_fakes import insights_settings
from joshua_core.engine import injection
from joshua_core.engine.injection import kb_context
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.recall import build_recall_server, do_search_memory
from joshua_core.engine.tools.recall import register as register_recall
from joshua_core.jobs.retrieval import ChatFilter
from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk
from joshua_core.store.models import Channel, Conversation
from snooze_fakes import TODAY, write_insight, write_open_question


@pytest.fixture(autouse=True)
def _const_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [0.1] * 384)


def _chunk(kind: str, path: str, sim: float) -> KbChunk:
    return KbChunk(
        person_id=None,
        source="files",
        kind=kind,
        path=path,
        title=path,
        heading="",
        text=f"text of {path}",
        similarity=sim,
    )


class World:
    """Pages on the disk and the chunks the store gives for them, the closed
    ones ranked highest."""

    def __init__(self, data: Path) -> None:
        self.data = data
        due = write_insight(data, "filter-change")
        ended = write_insight(data, "smoke-alarm", snoozed_until=TODAY - timedelta(days=1))
        dismissed = write_insight(data, "backup-check", status="dismissed")
        snoozed = write_insight(
            data, "gutter-clean", status="acknowledged", snoozed_until=TODAY + timedelta(days=3)
        )
        expired = write_insight(data, "roof-check", status="expired")
        answered = write_open_question(data, "boiler-service", status="answered")
        asked = write_open_question(data, "water-test")
        self.kept = [
            f"wiki/insights/{due}.md",
            f"wiki/insights/{ended}.md",
            f"wiki/questions/{asked}.md",
            "wiki/home/upkeep.md",
        ]
        self.dropped = [
            f"wiki/insights/{dismissed}.md",
            f"wiki/insights/{snoozed}.md",
            f"wiki/insights/{expired}.md",
            f"wiki/questions/{answered}.md",
            "wiki/insights/home/gone.md",
        ]
        self.chunks = [
            _chunk("insight", self.dropped[0], 0.99),
            _chunk("insight", self.dropped[1], 0.98),
            _chunk("insight", self.dropped[2], 0.97),
            _chunk("question", self.dropped[3], 0.96),
            _chunk("insight", self.dropped[4], 0.95),
            _chunk("insight", self.kept[0], 0.90),
            _chunk("insight", self.kept[1], 0.89),
            _chunk("question", self.kept[2], 0.88),
            _chunk("wiki", self.kept[3], 0.87),
        ]


class FakeStore:
    def __init__(self, chunks: list[KbChunk]) -> None:
        self.chunks = chunks
        self.k: list[int] = []

    async def kb_search(  # type: ignore[no-untyped-def]
        self, person_id, embedding, *, k, min_sim, sources=None, kinds=None, exclude_kinds=None
    ):
        self.k.append(k)
        return list(self.chunks)


class FakeRepo:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def add_kb_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


@pytest.fixture
def w(tmp_path: Path) -> World:
    return World(tmp_path / "data")


def test_the_filter_keeps_only_due_insights_and_open_questions(w: World) -> None:
    keep = ChatFilter(w.data, TODAY)
    assert [c.path for c in w.chunks if keep(c)] == w.kept


def test_the_filter_reads_each_page_once(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from joshua_core.jobs import retrieval

    reads: list[str] = []
    real = retrieval.load_insight

    def counted(insight_id: str, data_root: Any) -> Any:
        reads.append(insight_id)
        return real(insight_id, data_root)

    monkeypatch.setattr(retrieval, "load_insight", counted)
    keep = ChatFilter(w.data, TODAY)
    for _ in range(3):
        [keep(c) for c in w.chunks]
    assert len(reads) == len(set(reads)) == 6


def test_a_snooze_comes_back_after_its_last_day(w: World) -> None:
    snoozed = w.chunks[1]
    assert not ChatFilter(w.data, TODAY + timedelta(days=3))(snoozed)
    assert ChatFilter(w.data, TODAY + timedelta(days=4))(snoozed)


async def test_an_injected_turn_drops_closed_pages(w: World) -> None:
    store, repo = FakeStore(w.chunks), FakeRepo()
    ctx = SimpleNamespace(
        text="what upkeep is left at home",
        person_id="alex",
        conversation=SimpleNamespace(id="c1"),
        turn_id="t1",
    )
    note = await kb_context(
        ctx,
        store=store,
        repo=repo,
        embed_model="m",
        full_sim=0.72,
        hint_sim=0.60,
        top_k=4,
        max_chars=1200,
        recency_bonus=0.0,
        recency_half_life_days=14.0,
        per_doc_cap=3,
        keep=ChatFilter(w.data, TODAY),
    )
    assert note is not None
    for path in w.kept:
        assert f"read_file: {path})" in note
    for path in w.dropped:
        assert path not in note
    audited = [r["uri"] for r in repo.events[0]["results"]]
    assert audited == w.kept


async def test_the_registered_provider_uses_the_clock(w: World) -> None:
    class Manager:
        provider: Any = None

        def add_context_provider(self, provider: Any) -> None:
            self.provider = provider

    manager = Manager()
    store, repo = FakeStore(w.chunks), FakeRepo()
    # 03:00 UTC on the next day is still TODAY in the instance timezone.
    late = datetime.combine(TODAY + timedelta(days=1), datetime.min.time(), UTC)
    injection.register(
        manager,
        store,  # type: ignore[arg-type]
        insights_settings(),
        repo,  # type: ignore[arg-type]
        data_dir=w.data,
        clock=lambda: late + timedelta(hours=3),
    )
    ctx = SimpleNamespace(
        text="what upkeep is left at home",
        person_id="alex",
        conversation=SimpleNamespace(id="c1"),
        turn_id="t1",
    )
    await manager.provider(ctx)
    assert [r["uri"] for r in repo.events[0]["results"]] == w.kept


def _deps() -> ToolDeps:
    return ToolDeps(
        repo=None,  # type: ignore[arg-type]
        conversation=Conversation(id="c1", channel_id="telegram:1", person_id="alex"),
        channel=Channel(id="telegram:1", channel_type="telegram"),
        tz="UTC",
        person_id="alex",
    )


async def test_search_memory_drops_closed_pages_before_the_limit(w: World) -> None:
    store = FakeStore(w.chunks)
    results = await do_search_memory(
        _deps(),
        store=store,  # type: ignore[arg-type]
        embed_model="m",
        min_sim=0.5,
        query="upkeep",
        limit=4,
        recency_bonus=0.0,
        keep=ChatFilter(w.data, TODAY),
    )
    assert [r["path"] for r in results] == w.kept


async def test_search_memory_without_the_rule_keeps_every_page(w: World) -> None:
    results = await do_search_memory(
        _deps(),
        store=FakeStore(w.chunks),  # type: ignore[arg-type]
        embed_model="m",
        min_sim=0.5,
        query="upkeep",
        limit=20,
        recency_bonus=0.0,
    )
    assert len(results) == len(w.chunks)


async def test_the_registered_search_memory_tool_drops_closed_pages(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from joshua_core.engine.tools import recall as recall_module

    monkeypatch.setattr(recall_module, "create_sdk_mcp_server", lambda **kw: kw)

    class Manager:
        factory: Any = None

        def register_builtin(self, name: str, factory: Any) -> None:
            assert name == "recall"
            self.factory = factory

    manager = Manager()
    register_recall(
        manager,
        FakeStore(w.chunks),  # type: ignore[arg-type]
        insights_settings(),
        data_dir=w.data,
        clock=lambda: datetime(2026, 10, 19, 16, 0, tzinfo=UTC),
    )
    server = manager.factory(_deps())
    [handler] = [t.handler for t in server["tools"] if t.name == "search_memory"]
    reply = await handler({"query": "upkeep", "limit": 20})
    paths = [r["path"] for r in json.loads(reply["content"][0]["text"])]
    assert set(paths) == set(w.kept)
    unfiltered = build_recall_server(
        _deps(),
        store=FakeStore(w.chunks),  # type: ignore[arg-type]
        embed_model="m",
        min_sim=0.5,
        recency_bonus=0.0,
        recency_half_life_days=14.0,
        per_doc_cap=3,
    )
    [plain] = [t.handler for t in unfiltered["tools"] if t.name == "search_memory"]
    reply = await plain({"query": "upkeep", "limit": 20})
    assert len(json.loads(reply["content"][0]["text"])) == len(w.chunks)
