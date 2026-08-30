"""Integration tests for the memory index against a real pgvector database.

Covers the person-scope predicate, the incremental diff (edit/delete/unchanged),
and that unchanged files are not re-embedded. Embedding is monkeypatched to a
fixed non-zero vector with a call counter, so the tests are deterministic and do
not download a model; scope is enforced by SQL, not by vector distance.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_core.memory import embed as embed_module
from joshua_core.memory.indexer import Indexer
from joshua_core.memory.search import search
from joshua_core.memory.sources.files import FilesSource
from joshua_core.memory.store import MemoryStore

pytestmark = pytest.mark.integration

# A fixed non-zero vector: identical rows all score cosine 1.0, so retrieval is
# gated by the person predicate, not by content.
VEC = [1.0] + [0.0] * 383


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


def _tree(root: Path) -> None:
    (root / "people/alice/blog").mkdir(parents=True)
    (root / "people/bob/blog").mkdir(parents=True)
    (root / "wiki").mkdir(parents=True)
    (root / "shared").mkdir(parents=True)
    (root / "people/alice/blog/2026-08-01.md").write_text(
        "# Alice topic\n\nAlice's private journal notes about sailing.\n"
    )
    (root / "people/bob/blog/2026-08-02.md").write_text(
        "# Bob topic\n\nBob's private journal notes about cooking.\n"
    )
    (root / "wiki/w.md").write_text("# Wiki\n\nWiki notes about the boat, for everyone.\n")
    (root / "shared/s.md").write_text("# Shared\n\nShared notes about the guest wifi.\n")


def _indexer(pool, root: Path) -> tuple[MemoryStore, Indexer]:
    store = MemoryStore(pool)
    return store, Indexer(store, {"files": FilesSource(root)}, embed_model="m", chunk_chars=1600)


async def test_person_scope(db, repo, tmp_path: Path, count_embed) -> None:
    await _people(repo)
    _tree(tmp_path)
    store, indexer = _indexer(db.pool, tmp_path)
    await indexer.reindex()

    alice = {c.path for c in await search(store, "alice", VEC, k=10, min_sim=0.0)}
    assert {"blog/2026-08-01.md", "wiki/w.md", "shared/s.md"} <= alice
    assert "blog/2026-08-02.md" not in alice  # never sees Bob's rows

    bob = {c.path for c in await search(store, "bob", VEC, k=10, min_sim=0.0)}
    assert {"blog/2026-08-02.md", "wiki/w.md", "shared/s.md"} <= bob
    assert "blog/2026-08-01.md" not in bob

    group = {c.path for c in await search(store, None, VEC, k=10, min_sim=0.0)}
    assert group == {"wiki/w.md", "shared/s.md"}  # a group turn sees the wiki and shared


async def test_incremental_edit_delete_unchanged(db, repo, tmp_path: Path, count_embed) -> None:
    await _people(repo)
    _tree(tmp_path)
    store, indexer = _indexer(db.pool, tmp_path)

    stats = (await indexer.reindex())["files"]
    assert stats["indexed"] == 4
    after_first = count_embed["n"]
    assert after_first >= 4

    # Unchanged: nothing re-embedded on the next pass (sha check).
    stats = (await indexer.reindex())["files"]
    assert stats["changed"] == 0
    assert count_embed["n"] == after_first

    # Edit: the file's rows change within one pass.
    (tmp_path / "people/alice/blog/2026-08-01.md").write_text(
        "# Alice topic\n\nAlice edited her notes to be about hiking now.\n"
    )
    stats = (await indexer.reindex())["files"]
    assert stats["changed"] == 1 and count_embed["n"] > after_first
    rows = await store.kb_search("alice", VEC, k=10, min_sim=0.0)
    a_rows = [c for c in rows if c.path == "blog/2026-08-01.md"]
    assert a_rows and "hiking" in a_rows[0].text

    # Delete: the file's rows are removed.
    (tmp_path / "people/bob/blog/2026-08-02.md").unlink()
    stats = (await indexer.reindex())["files"]
    assert stats["removed"] == 1
    bob_rows = await store.kb_search("bob", VEC, k=10, min_sim=0.0)
    assert not any(c.path == "blog/2026-08-02.md" for c in bob_rows)
