"""Offline tests for per-turn KB injection.

``plan_injection`` and the note builders are pure; ``kb_context`` runs against a
FakeStore (canned chunks per scope) and a FakeRepo (records the audit row), with
the embedding model monkeypatched to a constant vector. No database.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from joshua_core.engine import injection
from joshua_core.engine.injection import kb_context, plan_injection
from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk


def _chunk(
    sim: float,
    path: str = "wiki/pizza.md",
    title: str = "Pizza",
    text: str = "Use 00 flour and a long cold rise.",
) -> KbChunk:
    return KbChunk(
        person_id="alice",
        source="files",
        kind="wiki",
        path=path,
        title=title,
        heading="Dough",
        text=text,
        similarity=sim,
    )


# --- plan_injection ----------------------------------------------------------


def test_plan_none_when_empty() -> None:
    plan = plan_injection([], inject_sim=0.72, top_k=2)
    assert plan.decision == "none"
    assert plan.injected == []
    assert plan.best_sim == 0.0


def test_plan_full_takes_top_k() -> None:
    ranked = [_chunk(0.9, "wiki/a.md"), _chunk(0.8, "wiki/b.md"), _chunk(0.75, "wiki/c.md")]
    plan = plan_injection(ranked, inject_sim=0.72, top_k=2)
    assert plan.decision == "full"
    assert [c.path for c in plan.injected] == ["wiki/a.md", "wiki/b.md"]
    assert plan.best_sim == 0.9


def test_plan_hint_below_full_and_distinct_docs() -> None:
    ranked = [
        _chunk(0.66, "wiki/a.md"),
        _chunk(0.64, "wiki/a.md"),  # same doc, deduped
        _chunk(0.63, "wiki/b.md"),
        _chunk(0.61, "wiki/c.md"),
        _chunk(0.60, "wiki/d.md"),  # over the three-doc cap
    ]
    plan = plan_injection(ranked, inject_sim=0.72, top_k=2)
    assert plan.decision == "hint"
    assert [c.path for c in plan.injected] == ["wiki/a.md", "wiki/b.md", "wiki/c.md"]


# --- note builders -----------------------------------------------------------


def test_full_note_has_header_and_read_file_path() -> None:
    note = injection._full_note([_chunk(0.9)], max_chars=1200)
    assert note.startswith("[From your notes and files")
    assert "read_file: wiki/pizza.md" in note
    assert "00 flour" in note


def test_full_note_caps_text() -> None:
    note = injection._full_note([_chunk(0.9, text="x" * 3000)], max_chars=100)
    assert "x" * 100 in note
    assert "x" * 101 not in note


def test_hint_note_names_files() -> None:
    note = injection._hint_note([_chunk(0.65, "wiki/pizza.md", "Pizza")])
    assert "read_file" in note
    assert "wiki/pizza.md (Pizza)" in note


# --- kb_context --------------------------------------------------------------


class FakeRepo:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def add_kb_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class FakeStore:
    """Serves the canned chunks for ``alice`` only; every other scope is empty."""

    def __init__(self, chunks: list[KbChunk]) -> None:
        self._chunks = chunks
        self.seen: list[str | None] = []
        self.exclude_seen: list[Any] = []

    async def kb_search(  # type: ignore[no-untyped-def]
        self, person_id, embedding, *, k, min_sim, sources=None, kinds=None, exclude_kinds=None
    ):
        self.seen.append(person_id)
        self.exclude_seen.append(exclude_kinds)
        if person_id != "alice":
            return []
        rows = self._chunks
        if exclude_kinds:
            rows = [c for c in rows if c.kind not in exclude_kinds]
        return list(rows)


class BoomStore:
    async def kb_search(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        raise RuntimeError("db down")


@pytest.fixture(autouse=True)
def _const_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [0.1] * 384)


def _ctx(text: str, person_id: str | None = "alice") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        person_id=person_id,
        conversation=SimpleNamespace(id="c1"),
        turn_id="t1",
    )


async def _run(ctx: Any, store: Any, repo: Any, **overrides: Any) -> str | None:
    kwargs: dict[str, Any] = dict(
        embed_model="m",
        full_sim=0.72,
        hint_sim=0.60,
        top_k=2,
        max_chars=1200,
        recency_bonus=0.08,
        recency_half_life_days=14.0,
        per_doc_cap=3,
    )
    kwargs.update(overrides)
    return await kb_context(ctx, store=store, repo=repo, **kwargs)


async def test_short_text_skips_retrieval_and_audit() -> None:
    store, repo = FakeStore([_chunk(0.9)]), FakeRepo()
    assert await _run(_ctx("hi"), store, repo) is None
    assert store.seen == []
    assert repo.events == []


async def test_embedding_unavailable_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": None)
    store, repo = FakeStore([_chunk(0.9)]), FakeRepo()
    assert await _run(_ctx("tell me about pizza"), store, repo) is None
    assert repo.events == []


async def test_retrieval_failure_proceeds_without_note() -> None:
    repo = FakeRepo()
    assert await _run(_ctx("tell me about pizza"), BoomStore(), repo) is None
    assert repo.events == []  # nothing to audit when retrieval never ran


async def test_full_injection_and_audit() -> None:
    store, repo = FakeStore([_chunk(0.9)]), FakeRepo()
    note = await _run(_ctx("tell me about pizza dough"), store, repo)
    assert note is not None
    assert "00 flour" in note
    event = repo.events[0]
    assert event["kind"] == "inject"
    assert event["decision"] == "full"
    assert event["conversation_id"] == "c1"
    assert event["turn_id"] == "t1"
    assert event["query"] == "tell me about pizza dough"
    assert event["best_sim"] == 0.9
    assert any(r["injected"] for r in event["results"])


async def test_other_person_gets_none_and_audit_row() -> None:
    store, repo = FakeStore([_chunk(0.9)]), FakeRepo()
    note = await _run(_ctx("tell me about pizza", person_id="bob"), store, repo)
    assert note is None
    assert store.seen == ["bob"]
    assert repo.events[0]["decision"] == "none"
    assert repo.events[0]["results"] == []


async def test_hint_names_file() -> None:
    store, repo = FakeStore([_chunk(0.65)]), FakeRepo()
    note = await _run(_ctx("something vague about food"), store, repo)
    assert note is not None
    assert "wiki/pizza.md" in note
    assert repo.events[0]["decision"] == "hint"


async def test_excludes_skill_rows_from_injection() -> None:
    skill = KbChunk(
        person_id="alice",
        source="files",
        kind="skill",
        path="wiki/skills/movie.md",
        title="Movie",
        heading="movie time",
        text="Dim the lights.",
        similarity=0.99,
    )
    store, repo = FakeStore([skill, _chunk(0.9)]), FakeRepo()
    note = await _run(_ctx("tell me about pizza dough"), store, repo)
    assert store.exclude_seen[0] == ("skill",)  # the exclude filter is forwarded
    assert note is not None
    assert "Dim the lights." not in note  # a skill row never reaches the note
    assert "00 flour" in note


async def test_stores_embedding_on_ctx() -> None:
    ctx = _ctx("tell me about pizza dough")
    await _run(ctx, FakeStore([_chunk(0.9)]), FakeRepo())
    assert ctx.embedding == [0.1] * 384  # shared with the taught-skill provider


# --- register ----------------------------------------------------------------


class FakeManager:
    def __init__(self) -> None:
        self.providers: list[Any] = []

    def add_context_provider(self, provider: Any) -> None:
        self.providers.append(provider)


def _settings(enabled: bool) -> Any:
    inject = SimpleNamespace(enabled=enabled, full_sim=0.72, hint_sim=0.60, top_k=2, max_chars=1200)
    memory = SimpleNamespace(
        inject=inject,
        embed_model="m",
        recency_bonus=0.08,
        recency_half_life_days=14.0,
        per_doc_cap=3,
    )
    return SimpleNamespace(memory=memory)


def test_register_wires_provider_when_enabled() -> None:
    manager = FakeManager()
    injection.register(manager, FakeStore([]), _settings(True), FakeRepo())
    assert len(manager.providers) == 1


def test_register_skips_when_disabled() -> None:
    manager = FakeManager()
    injection.register(manager, FakeStore([]), _settings(False), FakeRepo())
    assert manager.providers == []
