"""Integration tests for the memory index against a real pgvector database.

Covers the wiki-wide listing, the incremental diff (edit/delete/unchanged), and
that unchanged files are not re-embedded. Embedding is monkeypatched to a fixed
non-zero vector with a call counter, so the tests are deterministic and do not
download a model; every document is shared scope now, so ``person_id`` names
provenance, not a filter.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from joshua_core.memory import embed as embed_module
from joshua_core.memory.indexer import Indexer
from joshua_core.memory.search import search
from joshua_core.memory.sources.files import FilesSource
from joshua_core.memory.store import MemoryStore
from joshua_shared import layout

pytestmark = pytest.mark.integration

# A fixed non-zero vector: identical rows all score cosine 1.0, so retrieval is
# gated by scope, not by content.
VEC = [1.0] + [0.0] * 383

DAY_ONE = date(2026, 8, 1)
DAY_TWO = date(2026, 8, 2)


@pytest.fixture
def count_embed(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"n": 0}

    def fake_embed(text: str, model: str = "") -> list[float]:
        calls["n"] += 1
        return list(VEC)

    monkeypatch.setattr(embed_module, "embed", fake_embed)
    return calls


async def _people(repo) -> None:
    await repo.upsert_person("alice", "Alice")
    await repo.upsert_person("bob", "Bob")


def _entry_path(root: Path, day: date, slug: str) -> Path:
    path = layout.journal_entry_path(day, slug, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _tree(root: Path) -> None:
    _entry_path(root, DAY_ONE, "sailing").write_text(
        "# Alice topic\n\nAlice's journal entry about sailing.\n"
    )
    _entry_path(root, DAY_TWO, "cooking").write_text(
        "# Bob topic\n\nBob's journal entry about cooking.\n"
    )
    (root / "wiki").mkdir(parents=True, exist_ok=True)
    (root / "wiki/w.md").write_text("# Wiki\n\nWiki notes about the boat, for everyone.\n")


def _indexer(pool, root: Path) -> tuple[MemoryStore, Indexer]:
    store = MemoryStore(pool)
    return store, Indexer(store, {"files": FilesSource(root)}, embed_model="m", chunk_chars=1600)


async def test_a_search_reaches_the_whole_corpus(db, repo, tmp_path: Path, count_embed) -> None:
    """One corpus: a search is not narrowed by who asks.

    The wiki is what Joshua knows and the journal is when something happened,
    so a question about last Tuesday has to reach anyone's journal entry.
    """
    await _people(repo)
    _tree(tmp_path)
    store, indexer = _indexer(db.pool, tmp_path)
    await indexer.reindex()

    everything = {
        "wiki/journal/2026/08/01/sailing.md",
        "wiki/journal/2026/08/02/cooking.md",
        "wiki/w.md",
    }

    alice = {c.path for c in await search(store, "alice", VEC, k=10, min_sim=0.0)}
    bob = {c.path for c in await search(store, "bob", VEC, k=10, min_sim=0.0)}
    group = {c.path for c in await search(store, None, VEC, k=10, min_sim=0.0)}

    assert everything <= alice
    assert everything <= bob
    # A group turn reads the same corpus.
    assert everything <= group


async def test_journal_documents_are_shared_scope(db, repo, tmp_path: Path, count_embed) -> None:
    """``person_id`` is None on every files document now: the journal is
    Joshua's own, not a private per-person tree."""
    await _people(repo)
    _tree(tmp_path)
    store, indexer = _indexer(db.pool, tmp_path)
    await indexer.reindex()

    rows = {c.path: c.person_id for c in await search(store, "alice", VEC, k=10, min_sim=0.0)}
    assert rows["wiki/journal/2026/08/01/sailing.md"] is None
    assert rows["wiki/journal/2026/08/02/cooking.md"] is None
    assert rows["wiki/w.md"] is None


async def test_incremental_edit_delete_unchanged(db, repo, tmp_path: Path, count_embed) -> None:
    await _people(repo)
    _tree(tmp_path)
    store, indexer = _indexer(db.pool, tmp_path)

    stats = (await indexer.reindex())["files"]
    assert stats["indexed"] == 3
    after_first = count_embed["n"]
    assert after_first >= 3

    # Unchanged: nothing re-embedded on the next pass (sha check).
    stats = (await indexer.reindex())["files"]
    assert stats["changed"] == 0
    assert count_embed["n"] == after_first

    # Edit: the file's rows change within one pass.
    sailing = layout.journal_entry_path(DAY_ONE, "sailing", tmp_path)
    sailing.write_text("# Alice topic\n\nAlice edited her notes to be about hiking now.\n")
    stats = (await indexer.reindex())["files"]
    assert stats["changed"] == 1 and count_embed["n"] > after_first
    rows = await store.kb_search("alice", VEC, k=10, min_sim=0.0)
    a_rows = [c for c in rows if c.path == "wiki/journal/2026/08/01/sailing.md"]
    assert a_rows and "hiking" in a_rows[0].text

    # Delete: the file's rows are removed.
    layout.journal_entry_path(DAY_TWO, "cooking", tmp_path).unlink()
    stats = (await indexer.reindex())["files"]
    assert stats["removed"] == 1
    bob_rows = await store.kb_search("bob", VEC, k=10, min_sim=0.0)
    assert not any(c.path == "wiki/journal/2026/08/02/cooking.md" for c in bob_rows)
