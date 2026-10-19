"""`MemoryStore.kb_near_paths` against a real pgvector database.

It must find every journal document with a chunk at or above the floor, report
its best similarity, and leave out other kinds and other sources.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from joshua_core.memory.store import MemoryStore

pytestmark = pytest.mark.integration

MTIME = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _vec(*head: float) -> list[float]:
    return [*head, *([0.0] * (384 - len(head)))]


async def _put(store: MemoryStore, path: str, kind: str, vectors, source: str = "files") -> None:
    await store.kb_replace_item(
        source=source,
        person_id=None,
        path=path,
        kind=kind,
        title=path,
        provenance="own",
        file_sha256="sha",
        file_mtime=MTIME,
        doc_date=None,
        chunks=[("", f"chunk {i}", v) for i, v in enumerate(vectors)],
    )


async def test_kb_near_paths_finds_every_document_above_the_floor(db) -> None:
    store = MemoryStore(db.pool)
    await _put(store, "wiki/journal/2026/10/01/a.md", "journal", [_vec(1.0), _vec(0.0, 1.0)])
    await _put(store, "wiki/journal/2026/10/02/b.md", "journal", [_vec(0.0, 0.0, 1.0)])
    await _put(store, "wiki/journal/2026/10/03/c.md", "journal", [_vec(0.0, 1.0)])
    await _put(store, "wiki/houseplants/pothos.md", "wiki", [_vec(1.0)])
    await _put(store, "memos/x", "journal", [_vec(1.0)], source="memos")

    got = await store.kb_near_paths([_vec(1.0), _vec(0.0, 1.0)], kinds=["journal"], min_sim=0.7)
    assert set(got) == {"wiki/journal/2026/10/01/a.md", "wiki/journal/2026/10/03/c.md"}
    assert got["wiki/journal/2026/10/01/a.md"] == pytest.approx(1.0)


async def test_kb_near_paths_with_nothing_to_ask(db) -> None:
    store = MemoryStore(db.pool)
    assert await store.kb_near_paths([], kinds=["journal"], min_sim=0.7) == {}
    assert await store.kb_near_paths([_vec(1.0)], kinds=[], min_sim=0.7) == {}
