"""Offline tests for the indexer diff-and-repair logic.

A FakeStore records writes/deletes and a FakeSource serves canned documents;
embedding is monkeypatched to a constant vector with a call counter, so the
"unchanged files are not re-embedded" rule is asserted by call count.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from joshua_core.memory import embed as embed_module
from joshua_core.memory.indexer import Indexer
from joshua_core.memory.sources import Document

MTIME = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _doc(person: str | None, path: str, rev: str) -> Document:
    return Document(
        source="files",
        uri=path,
        person_id=person,
        title=f"T {path}",
        text="Body text long enough to become a standalone chunk of documentation.",
        updated_at=MTIME,
        rev=rev,
    )


class FakeSource:
    name = "files"

    def __init__(self, docs: list[Document]):
        self.docs = docs
        self.raise_on_list = False

    async def list_documents(self) -> AsyncIterator[Document]:
        if self.raise_on_list:
            raise TimeoutError()  # str() is "" — reindex must fall back to repr
        for doc in self.docs:
            yield doc

    async def fetch(self, uri: str) -> Document | None:
        return next((d for d in self.docs if d.uri == uri), None)


class FakeStore:
    def __init__(self, state: dict[tuple[str | None, str], str] | None = None):
        self.state = state or {}
        self.replaced: list[tuple[str | None, str]] = []
        self.deleted: list[tuple[str | None, str]] = []
        self.purged: list[list[str]] = []

    async def kb_index_state(self, source: str):
        return dict(self.state)

    async def kb_replace_item(
        self,
        *,
        source,
        person_id,
        path,
        kind,
        title,
        provenance,
        file_sha256,
        file_mtime,
        doc_date,
        chunks,
    ):
        self.replaced.append((person_id, path))
        return len(list(chunks))

    async def kb_delete_item(self, source, person_id, path):
        self.deleted.append((person_id, path))
        return 1

    async def kb_purge_sources(self, keep):
        self.purged.append(list(keep))
        return 0


@pytest.fixture(autouse=True)
def _count_embeds(monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}

    def fake_embed(text: str, model: str = "") -> list[float]:
        calls["n"] += 1
        return [0.0] * 384

    monkeypatch.setattr(embed_module, "embed", fake_embed)
    return calls


def _indexer(store: FakeStore, docs: list[Document]) -> Indexer:
    return Indexer(
        store,
        {"files": FakeSource(docs)},  # type: ignore[arg-type]
        embed_model="m",
        chunk_chars=1600,
    )


async def test_bootstrap_indexes_everything(_count_embeds) -> None:
    docs = [_doc("alice", "wiki/a.md", "r1"), _doc(None, "shared/s.md", "r2")]
    store = FakeStore()
    stats = (await _indexer(store, docs).reindex())["files"]
    assert stats == {"listed": 2, "changed": 2, "indexed": 2, "removed": 0, "failed": 0}
    assert set(store.replaced) == {(None, "shared/s.md"), ("alice", "wiki/a.md")}
    assert store.purged == [["files"]]


async def test_unchanged_documents_are_not_reembedded(_count_embeds) -> None:
    docs = [_doc("alice", "wiki/a.md", "r1")]
    store = FakeStore(state={("alice", "wiki/a.md"): "r1"})
    stats = (await _indexer(store, docs).reindex())["files"]
    assert stats["changed"] == 0
    assert store.replaced == []
    assert _count_embeds["n"] == 0  # sha matched → nothing embedded


async def test_edit_and_delete(_count_embeds) -> None:
    docs = [_doc("alice", "wiki/a.md", "r2")]  # a.md edited; ghost.md vanished
    store = FakeStore(state={("alice", "wiki/a.md"): "r1", ("alice", "wiki/ghost.md"): "rX"})
    stats = (await _indexer(store, docs).reindex())["files"]
    assert stats["changed"] == 1 and store.replaced == [("alice", "wiki/a.md")]
    assert store.deleted == [("alice", "wiki/ghost.md")]


async def test_force_reindexes_all(_count_embeds) -> None:
    docs = [_doc("alice", "wiki/a.md", "r1")]
    store = FakeStore(state={("alice", "wiki/a.md"): "r1"})
    stats = (await _indexer(store, docs).reindex(full=True))["files"]
    assert stats["indexed"] == 1 and _count_embeds["n"] > 0


async def test_person_filter_scopes_the_pass(_count_embeds) -> None:
    docs = [_doc("alice", "wiki/a.md", "r1"), _doc("bob", "wiki/b.md", "r1")]
    store = FakeStore()
    stats = (await _indexer(store, docs).reindex(person="alice"))["files"]
    assert stats["indexed"] == 1 and store.replaced == [("alice", "wiki/a.md")]


async def test_embedding_failure_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(text: str, model: str = "") -> None:
        return None  # embedding unavailable → _index_document raises, doc skipped

    monkeypatch.setattr(embed_module, "embed", boom)
    docs = [_doc("alice", "wiki/a.md", "r1"), _doc("alice", "wiki/b.md", "r1")]
    store = FakeStore()
    stats = (await _indexer(store, docs).reindex())["files"]
    assert stats["failed"] == 2 and stats["indexed"] == 0


async def test_listing_failure_is_recorded_not_raised(_count_embeds) -> None:
    store = FakeStore()
    indexer = Indexer(store, {"files": FakeSource([])}, embed_model="m", chunk_chars=1600)  # type: ignore[arg-type]
    indexer.sources["files"].raise_on_list = True  # type: ignore[attr-defined]
    results = await indexer.reindex()
    assert "error" in results["files"] and results["files"]["error"]
    assert indexer.status()["files"]["last_error"]


async def test_unknown_source_reported(_count_embeds) -> None:
    store = FakeStore()
    indexer = Indexer(store, {"files": FakeSource([])}, embed_model="m", chunk_chars=1600)  # type: ignore[arg-type]
    results = await indexer.reindex(source="memos")
    assert results == {"memos": {"error": "unknown source"}}
