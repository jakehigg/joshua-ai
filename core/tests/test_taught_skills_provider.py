"""Offline tests for the taught-skill context provider.

A FakeStore serves canned skill rows and records the kb_search kwargs; a
FakeRepo records the audit row. The embedding model is monkeypatched to a
constant vector. No database.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from joshua_core.engine import taught_skills
from joshua_core.engine.taught_skills import taught_skill_context
from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk


def _row(sim: float, path: str, trigger: str, text: str, title: str = "Movie time") -> KbChunk:
    return KbChunk(
        person_id=None,
        source="files",
        kind="skill",
        path=path,
        title=title,
        heading=trigger,
        text=text,
        similarity=sim,
    )


class FakeRepo:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def add_kb_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class FakeStore:
    """Serves canned rows above ``min_sim`` and records the kb_search kwargs."""

    def __init__(self, rows: list[KbChunk]) -> None:
        self._rows = rows
        self.calls: list[dict[str, Any]] = []
        self.embeds: list[list[float]] = []

    async def kb_search(  # type: ignore[no-untyped-def]
        self, person_id, embedding, *, k, min_sim, sources=None, kinds=None, exclude_kinds=None
    ):
        self.calls.append({"person_id": person_id, "k": k, "min_sim": min_sim, "kinds": kinds})
        self.embeds.append(embedding)
        rows = [r for r in self._rows if (r.similarity or 0.0) >= min_sim]
        rows.sort(key=lambda r: -(r.similarity or 0.0))
        return rows[:k]


class BoomStore:
    async def kb_search(self, *a: Any, **k: Any):  # type: ignore[no-untyped-def]
        raise RuntimeError("db down")


@pytest.fixture(autouse=True)
def _const_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [0.2] * 384)


def _ctx(text: str, person_id: str | None = "alice", embedding: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        person_id=person_id,
        conversation=SimpleNamespace(id="c1"),
        turn_id="t1",
        embedding=embedding,
    )


async def _run(ctx: Any, store: Any, repo: Any, *, min_sim: float = 0.62, top_k: int = 2):
    return await taught_skill_context(
        ctx, store=store, repo=repo, embed_model="m", min_sim=min_sim, top_k=top_k
    )


async def test_match_returns_block_and_audit() -> None:
    store = FakeStore([_row(0.9, "wiki/skills/movie.md", "movie time", "Dim the lights.")])
    repo = FakeRepo()
    note = await _run(_ctx("let's watch a movie"), store, repo)
    assert note is not None
    assert note.startswith('[Taught skill "Movie time" matches this request')
    assert "Dim the lights." in note
    assert store.calls[0]["kinds"] == ("skill",)
    event = repo.events[0]
    assert event["kind"] == "skill"
    assert event["decision"] == "match"
    assert event["best_sim"] == 0.9
    assert any(r["injected"] for r in event["results"])


async def test_no_match_below_floor_returns_none_and_audits() -> None:
    store = FakeStore([_row(0.5, "wiki/skills/movie.md", "movie time", "Dim the lights.")])
    repo = FakeRepo()
    assert await _run(_ctx("what is the weather"), store, repo) is None
    assert repo.events[0]["decision"] == "none"


async def test_top_k_limits_and_dedups_by_skill() -> None:
    rows = [
        _row(0.95, "wiki/skills/movie.md", "movie time", "Dim.", "Movie"),
        _row(0.90, "wiki/skills/movie.md", "watch a movie", "Dim.", "Movie"),  # same skill
        _row(0.85, "wiki/skills/party.md", "party mode", "Music on.", "Party"),
        _row(0.80, "wiki/skills/coffee.md", "coffee", "Brew.", "Coffee"),
    ]
    store = FakeStore(rows)
    note = await _run(_ctx("start a movie"), store, FakeRepo(), top_k=2)
    assert note is not None
    # Two distinct skills, best first; the duplicate movie trigger fires once.
    assert note.count("Taught skill") == 2
    assert '"Movie"' in note and '"Party"' in note
    assert '"Coffee"' not in note


async def test_reuses_ctx_embedding_and_does_not_reembed() -> None:
    store = FakeStore([_row(0.9, "wiki/skills/movie.md", "movie time", "Dim.")])
    ctx = _ctx("let's watch a movie", embedding=[0.5] * 384)
    await _run(ctx, store, FakeRepo())
    assert store.embeds == [[0.5] * 384]  # the provided vector, not a fresh embed


async def test_embeds_when_ctx_embedding_absent() -> None:
    store = FakeStore([_row(0.9, "wiki/skills/movie.md", "movie time", "Dim.")])
    ctx = _ctx("let's watch a movie", embedding=None)
    await _run(ctx, store, FakeRepo())
    assert store.embeds == [[0.2] * 384]  # embedded from the constant fixture
    assert ctx.embedding == [0.2] * 384  # stored back for reuse


async def test_embedding_unavailable_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": None)
    repo = FakeRepo()
    store = FakeStore([_row(0.9, "wiki/skills/movie.md", "movie time", "Dim.")])
    assert await _run(_ctx("let's watch a movie"), store, repo) is None
    assert repo.events == []  # nothing searched, nothing audited


async def test_retrieval_failure_returns_none() -> None:
    repo = FakeRepo()
    assert await _run(_ctx("let's watch a movie"), BoomStore(), repo) is None
    assert repo.events == []


async def test_empty_text_returns_none() -> None:
    store = FakeStore([_row(0.9, "wiki/skills/movie.md", "movie time", "Dim.")])
    assert await _run(_ctx("   "), store, FakeRepo()) is None
    assert store.calls == []


# --- register ----------------------------------------------------------------


class FakeManager:
    def __init__(self) -> None:
        self.providers: list[Any] = []

    def add_context_provider(self, provider: Any) -> None:
        self.providers.append(provider)


def _settings(enabled: bool) -> Any:
    skills = SimpleNamespace(enabled=enabled, min_sim=0.62, top_k=2)
    memory = SimpleNamespace(skills=skills, embed_model="m")
    return SimpleNamespace(memory=memory)


def test_register_wires_provider_when_enabled() -> None:
    manager = FakeManager()
    taught_skills.register(manager, FakeStore([]), _settings(True), FakeRepo())
    assert len(manager.providers) == 1


def test_register_skips_when_disabled() -> None:
    manager = FakeManager()
    taught_skills.register(manager, FakeStore([]), _settings(False), FakeRepo())
    assert manager.providers == []


async def test_default_top_k_fires_the_best_skill_alone() -> None:
    """Two skills above the floor: the better one fires, the runner-up does not.

    Only the trigger is embedded, so two skills that answer the same shape of
    message sit close together. On the instance "here's a plant" and "here's a
    receipt" measured 0.72, above the 0.62 floor, and both fired.
    """
    rows = [
        _row(0.81, "wiki/skills/receipt.md", "here's a receipt", "File it.", "Receipt"),
        _row(0.72, "wiki/skills/plant.md", "here's a plant", "Log it.", "Plant"),
    ]
    note = await _run(_ctx("here's a receipt"), FakeStore(rows), FakeRepo(), top_k=1)
    assert note is not None
    assert note.count("Taught skill") == 1
    assert '"Receipt"' in note and '"Plant"' not in note
