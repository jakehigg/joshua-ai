"""Integration tests for per-turn injection against a real pgvector database.

Covers the full-injection path end to end (index → embed → search → note →
``kb_event`` audit) and the person-scope predicate. Embedding is monkeypatched to
a fixed non-zero vector, so every indexed row scores cosine 1.0 and scope, not
content, decides what a person retrieves.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from joshua_core.engine.injection import kb_context
from joshua_core.memory import embed as embed_module
from joshua_core.memory.indexer import Indexer
from joshua_core.memory.sources.files import FilesSource
from joshua_core.memory.store import MemoryStore

pytestmark = pytest.mark.integration

VEC = [1.0] + [0.0] * 383


@pytest.fixture
def const_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": list(VEC))


def _tree(root: Path) -> None:
    (root / "people/alice/blog").mkdir(parents=True)
    (root / "people/bob/blog").mkdir(parents=True)
    (root / "shared").mkdir(parents=True)
    (root / "people/alice/blog/2026-08-20.md").write_text(
        "# Pizza dough\n\nUse 00 flour and give the dough a long cold rise.\n"
    )
    (root / "people/bob/blog/2026-08-21.md").write_text(
        "# Cooking\n\nBob keeps his private stir-fry notes here.\n"
    )
    (root / "shared/house.md").write_text("# House\n\nThe guest wifi password is on the fridge.\n")


async def _indexed(pool, root: Path) -> MemoryStore:
    store = MemoryStore(pool)
    indexer = Indexer(store, {"files": FilesSource(root)}, embed_model="m", chunk_chars=1600)
    await indexer.reindex()
    return store


async def _ctx(repo, channel: str, person_id: str | None, text: str) -> SimpleNamespace:
    await repo.upsert_channel(channel, "telegram", default_person_id=person_id)
    conv = await repo.get_or_create_conversation(channel, person_id)
    return SimpleNamespace(
        text=text, person_id=person_id, conversation=SimpleNamespace(id=conv.id), turn_id="t-x"
    )


async def _run(ctx: Any, store: MemoryStore, repo) -> str | None:
    return await kb_context(
        ctx,
        store=store,
        repo=repo,
        embed_model="m",
        full_sim=0.72,
        hint_sim=0.60,
        top_k=2,
        max_chars=1200,
        recency_bonus=0.08,
        recency_half_life_days=14.0,
        per_doc_cap=3,
    )


async def test_full_injection_and_audit_row(db, repo, tmp_path: Path, const_embed) -> None:
    await repo.upsert_person("alice", "Alice")
    await repo.upsert_person("bob", "Bob")
    _tree(tmp_path)
    store = await _indexed(db.pool, tmp_path)

    ctx = await _ctx(repo, "telegram:alice", "alice", "tell me about pizza dough")
    note = await _run(ctx, store, repo)
    assert note is not None
    assert note.startswith("[From your notes and files")
    assert "00 flour" in note

    events = await repo.kb_events(10)
    assert events[0]["decision"] == "full"
    assert events[0]["turn_id"] == "t-x"
    uris = {r["uri"] for r in events[0]["results"]}
    assert "blog/2026-08-20.md" in uris
    # One corpus: the journal of another person is in scope, and the audit row
    # records every document the search considered.
    assert "blog/2026-08-21.md" in uris
    assert any(r["injected"] and r["uri"] == "blog/2026-08-20.md" for r in events[0]["results"])


async def test_a_group_turn_reaches_the_same_corpus(db, repo, tmp_path: Path, const_embed) -> None:
    """A group turn is not a narrower turn.

    The corpus is shared, so a family chat can answer a question about
    anybody's week.
    """
    await repo.upsert_person("alice", "Alice")
    await repo.upsert_person("bob", "Bob")
    _tree(tmp_path)
    store = await _indexed(db.pool, tmp_path)

    bob_ctx = await _ctx(repo, "telegram:bob", "bob", "what did I note about cooking")
    bob_note = await _run(bob_ctx, store, repo)
    assert bob_note is not None
    assert "stir-fry" in bob_note

    group_ctx = await _ctx(repo, "telegram:group", None, "what is the guest wifi password")
    group_note = await _run(group_ctx, store, repo)
    assert group_note is not None
    events = await repo.kb_events(1)
    uris = {r["uri"] for r in events[0]["results"]}
    # A journal reaches this set, and not ``shared/house.md`` alone.
    assert any(uri.startswith("blog/") for uri in uris)
    # The fixture embeds every document to the same vector, so each one ties and
    # the top-k order is arbitrary. Which document wins is not asserted: the
    # scope is what this test is about. ``test_a_search_reaches_the_whole_corpus``
    # proves the shared files are still reachable.
