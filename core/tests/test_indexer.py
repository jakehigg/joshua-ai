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
        # The last replace call's kind, title, and (heading, text) chunks.
        self.last_replace: dict = {}

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
        chunk_list = list(chunks)
        self.last_replace = {
            "kind": kind,
            "title": title,
            "chunks": [(h, t) for h, t, _ in chunk_list],
        }
        return len(chunk_list)

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
    assert stats == {
        "listed": 2,
        "changed": 2,
        "indexed": 2,
        "removed": 0,
        "failed": 0,
        "skipped": 0,
        "unindexable": 0,
    }
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


# --- taught skills -----------------------------------------------------------


def _skill_doc(path: str, frontmatter: dict[str, str], body: str, rev: str = "r1") -> Document:
    return Document(
        source="files",
        uri=path,
        person_id=None,
        title="ignored for skills",
        text=body,
        updated_at=MTIME,
        rev=rev,
        frontmatter=frontmatter,
    )


async def test_skill_document_one_chunk_per_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    embedded: list[str] = []

    def capture(text: str, model: str = "") -> list[float]:
        embedded.append(text)
        return [0.0] * 384

    monkeypatch.setattr(embed_module, "embed", capture)
    doc = _skill_doc(
        "wiki/skills/movie.md",
        {"name": "movie time", "triggers": '["movie time", "let\'s watch a movie"]'},
        "Dim the lights and turn on the TV.",
    )
    store = FakeStore()
    await _indexer(store, [doc]).reindex()
    assert store.last_replace["kind"] == "skill"
    assert store.last_replace["title"] == "movie time"
    # One chunk per trigger: heading is the trigger, text is the full instructions.
    assert store.last_replace["chunks"] == [
        ("movie time", "Dim the lights and turn on the TV."),
        ("let's watch a movie", "Dim the lights and turn on the TV."),
    ]
    # The bare trigger phrase is embedded — no title breadcrumb.
    assert embedded == ["movie time", "let's watch a movie"]


async def test_skill_title_defaults_to_slug(_count_embeds) -> None:
    doc = _skill_doc("wiki/skills/coffee.md", {"triggers": '["coffee"]'}, "Brew a cup.")
    store = FakeStore()
    await _indexer(store, [doc]).reindex()
    assert store.last_replace["title"] == "coffee"


async def test_broken_skill_skipped_others_still_index(_count_embeds, caplog) -> None:
    good = _doc("alice", "wiki/a.md", "r1")
    broken = _skill_doc("wiki/skills/bad.md", {}, "No frontmatter body.")  # no triggers
    store = FakeStore()
    with caplog.at_level("WARNING"):
        stats = (await _indexer(store, [good, broken]).reindex())["files"]
    assert stats["indexed"] == 2 and stats["failed"] == 0
    assert ("alice", "wiki/a.md") in store.replaced
    assert store.last_replace["chunks"] != []  # the good doc still wrote chunks
    assert any("wiki/skills/bad.md" in r.getMessage() for r in caplog.records)


async def test_empty_triggers_writes_no_rows(_count_embeds) -> None:
    doc = _skill_doc("wiki/skills/off.md", {"triggers": "[]"}, "Instructions.")
    store = FakeStore()
    await _indexer(store, [doc]).reindex()
    assert (None, "wiki/skills/off.md") in store.replaced
    assert store.last_replace["chunks"] == []


async def test_lost_model_logs_one_line_for_the_pass(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A lost model must not write one warning for each document.

    Eleven thousand identical lines hid the cause of a real outage. The pass
    reports the count one time instead.
    """
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": None)
    monkeypatch.setattr(embed_module, "is_available", lambda: False)
    docs = [_doc("alice", f"wiki/{n}.md", "r1") for n in range(5)]

    with caplog.at_level("WARNING"):
        stats = (await _indexer(FakeStore(), docs).reindex())["files"]

    assert stats["failed"] == 5
    per_doc = [r for r in caplog.records if "kb index failed" in r.getMessage()]
    per_pass = [r for r in caplog.records if "kb index failed for the pass" in r.getMessage()]
    assert per_doc == per_pass, "no per-document warning may survive a lost model"
    assert len(per_pass) == 1
    assert "failed" in per_pass[0].getMessage()


async def test_one_bad_document_keeps_its_own_warning(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """With the model up, a single failure is that document's fault: name it."""
    monkeypatch.setattr(embed_module, "is_available", lambda: True)

    docs = [_doc("alice", "wiki/a.md", "r1"), _doc("alice", "wiki/b.md", "r1")]
    monkeypatch.setattr(
        embed_module,
        "embed",
        lambda text, model="": None if "wiki/b.md" in text else [0.1] * 384,
    )

    with caplog.at_level("WARNING"):
        await _indexer(FakeStore(), docs).reindex()

    per_doc = [r for r in caplog.records if "kb index failed" in r.getMessage()]
    assert any("wiki/b.md" in r.getMessage() for r in per_doc)
    assert not any("for the pass" in r.getMessage() for r in per_doc)


# -- a document that cannot be stored ------------------------------------------


class ConstraintError(Exception):
    """A psycopg error carries the SQLSTATE. Class 23 is a constraint violation."""

    sqlstate = "23503"


class RefusingStore(FakeStore):
    """Refuses one path the way Postgres refuses a person id that names no row."""

    def __init__(self, refuse: str, **kwargs):
        super().__init__(**kwargs)
        self.refuse = refuse
        self.attempts: list[str] = []

    async def kb_replace_item(self, **kwargs):
        self.attempts.append(kwargs["path"])
        if kwargs["path"] == self.refuse:
            raise ConstraintError("kb_chunk_person_id_fkey")
        return await super().kb_replace_item(**kwargs)


async def test_a_document_that_breaks_a_constraint_is_not_retried(_count_embeds) -> None:
    """The revision is never stored, so every pass saw it as changed and failed it."""
    docs = [_doc("ghost", "blog/x.md", "r1"), _doc("alice", "wiki/a.md", "r1")]
    store = RefusingStore("blog/x.md")
    indexer = _indexer(store, docs)

    first = (await indexer.reindex())["files"]
    assert first["failed"] == 1 and first["indexed"] == 1

    store.state = {("alice", "wiki/a.md"): "r1"}
    second = (await indexer.reindex())["files"]
    assert second["failed"] == 0
    assert second["skipped"] == 1
    assert store.attempts.count("blog/x.md") == 1


async def test_a_held_document_is_retried_when_its_revision_changes(_count_embeds) -> None:
    docs = [_doc("ghost", "blog/x.md", "r1")]
    store = RefusingStore("blog/x.md")
    indexer = _indexer(store, docs)
    await indexer.reindex()

    docs[0] = _doc("ghost", "blog/x.md", "r2")
    await indexer.reindex()
    assert store.attempts.count("blog/x.md") == 2


async def test_a_full_reindex_tries_a_held_document_again(_count_embeds) -> None:
    """An operator asking for the whole source is asking for this one too."""
    docs = [_doc("ghost", "blog/x.md", "r1")]
    store = RefusingStore("blog/x.md")
    indexer = _indexer(store, docs)
    await indexer.reindex()
    await indexer.reindex(full=True)
    assert store.attempts.count("blog/x.md") == 2


async def test_the_status_names_the_condition_and_the_count(_count_embeds) -> None:
    docs = [_doc("ghost", "blog/x.md", "r1")]
    indexer = _indexer(RefusingStore("blog/x.md"), docs)
    await indexer.reindex()

    held = indexer.status()["files"]["unindexable"]
    assert held["count"] == 1
    assert held["paths"] == ["blog/x.md"]


async def test_a_held_document_logs_one_line_and_not_one_a_pass(_count_embeds, caplog) -> None:
    """Roughly 2,880 identical lines a day is the shape this replaces."""
    docs = [_doc("ghost", "blog/x.md", "r1")]
    indexer = _indexer(RefusingStore("blog/x.md"), docs)
    with caplog.at_level("WARNING"):
        await indexer.reindex()
        await indexer.reindex()
        await indexer.reindex()
    lines = [r for r in caplog.records if "unindexable" in r.getMessage()]
    assert len(lines) == 1


async def test_a_fault_of_the_run_is_retried(_count_embeds, monkeypatch) -> None:
    """A lost connection is not the document's fault; the next pass tries again."""
    monkeypatch.setattr(embed_module, "is_available", lambda: True)

    class FlakyStore(FakeStore):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def kb_replace_item(self, **kwargs):
            self.attempts += 1
            raise OSError("connection lost")

    store = FlakyStore()
    indexer = _indexer(store, [_doc("alice", "wiki/a.md", "r1")])
    await indexer.reindex()
    await indexer.reindex()
    assert store.attempts == 2
    assert indexer.status()["files"]["unindexable"]["count"] == 0


async def test_a_document_that_vanishes_leaves_no_entry_behind(_count_embeds) -> None:
    docs = [_doc("ghost", "blog/x.md", "r1")]
    indexer = _indexer(RefusingStore("blog/x.md"), docs)
    await indexer.reindex()
    assert indexer.status()["files"]["unindexable"]["count"] == 1

    docs.clear()
    await indexer.reindex()
    assert indexer.status()["files"]["unindexable"]["count"] == 0
