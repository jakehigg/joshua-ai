"""The in-memory skill registry, and the indexer pass that fills it.

The phrase match reads the registry, not the database, so the registry has to
hold every skill on the volume after each pass — including the files the diff
decided it did not need to re-embed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from joshua_core.memory import embed as embed_module
from joshua_core.memory.indexer import Indexer
from joshua_core.memory.skill_registry import SkillRegistry
from joshua_core.memory.skills import (
    KIND_COMMAND,
    KIND_CONVENTION,
    MATCH_PHRASE,
    MATCH_SEMANTIC,
    Audience,
    Skill,
)
from joshua_core.memory.sources import Document

MTIME = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _skill(path: str, *, kind: str = KIND_COMMAND, match: str = MATCH_PHRASE) -> Skill:
    return Skill(
        name=path,
        triggers=("movie time",) if kind == KIND_COMMAND else (),
        instructions="Do the thing.",
        kind=kind,
        audience=Audience(),
        match=match,
        path=path,
    )


# --- the registry itself ------------------------------------------------------


def test_a_new_registry_is_empty_and_says_so() -> None:
    registry = SkillRegistry()
    assert not registry.loaded
    assert registry.all() == ()
    assert registry.phrase_skills() == ()


def test_replace_swaps_the_whole_set() -> None:
    registry = SkillRegistry()
    registry.replace([_skill("a.md")])
    assert [s.path for s in registry.all()] == ["a.md"]
    # A second pass replaces, it does not add.
    registry.replace([_skill("b.md")])
    assert [s.path for s in registry.all()] == ["b.md"]
    assert registry.loaded


def test_the_registry_sorts_pages_by_how_they_fire() -> None:
    registry = SkillRegistry()
    registry.replace(
        [
            _skill("phrase.md"),
            _skill("semantic.md", match=MATCH_SEMANTIC),
            _skill("convention.md", kind=KIND_CONVENTION),
        ]
    )
    assert [s.path for s in registry.phrase_skills()] == ["phrase.md"]
    assert registry.semantic_paths() == frozenset({"semantic.md"})


def test_by_path_finds_a_skill_or_returns_none() -> None:
    registry = SkillRegistry()
    registry.replace([_skill("a.md")])
    assert registry.by_path("a.md") is not None
    assert registry.by_path("gone.md") is None


# --- the indexer pass that fills it -------------------------------------------


def _page(path: str, frontmatter: dict[str, str], rev: str = "r1") -> Document:
    return Document(
        source="files",
        uri=path,
        person_id=None,
        title=path,
        text="Turn the lamp on.",
        updated_at=MTIME,
        rev=rev,
        frontmatter=frontmatter,
    )


class FakeSource:
    name = "files"

    def __init__(self, docs: list[Document]):
        self.docs = docs

    async def list_documents(self) -> AsyncIterator[Document]:
        for doc in self.docs:
            yield doc

    async def fetch(self, uri: str) -> Document | None:
        return None


class FakeStore:
    def __init__(self) -> None:
        self.written: list[dict[str, Any]] = []
        self.state: dict[tuple[str | None, str], str] = {}

    async def kb_index_state(self, source: str) -> dict[tuple[str | None, str], str]:
        return dict(self.state)

    async def kb_replace_item(self, **kwargs: Any) -> int:
        self.written.append(kwargs)
        self.state[(kwargs["person_id"], kwargs["path"])] = kwargs["file_sha256"]
        return len(kwargs.get("chunks", []))

    async def kb_delete_item(self, source: str, person_id: str | None, path: str) -> int:
        return 0


@pytest.fixture(autouse=True)
def _const_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [0.1] * 384)
    monkeypatch.setattr(embed_module, "is_available", lambda: True)


def _indexer(source: FakeSource, store: FakeStore) -> Indexer:
    return Indexer(store, {"files": source}, embed_model="m", chunk_chars=400)


async def test_a_pass_fills_the_registry_from_every_skill_page() -> None:
    source = FakeSource(
        [
            _page("wiki/skills/movie.md", {"name": "movie", "triggers": "[movie time]"}),
            _page("wiki/skills/rules.md", {"name": "rules", "kind": "convention"}),
            _page("wiki/notes/other.md", {}),
        ]
    )
    indexer = _indexer(source, FakeStore())
    await indexer.reindex()
    paths = {s.path for s in indexer.skills.all()}
    assert paths == {"wiki/skills/movie.md", "wiki/skills/rules.md"}
    assert [s.path for s in indexer.skills.phrase_skills()] == ["wiki/skills/movie.md"]


async def test_an_unchanged_page_still_reaches_the_registry() -> None:
    """The diff skips re-embedding, but the matcher still needs the phrase.

    A restart, or any pass where nothing changed, must not leave the registry
    short of the skills that were already indexed.
    """
    page = _page("wiki/skills/movie.md", {"name": "movie", "triggers": "[movie time]"})
    store = FakeStore()
    source = FakeSource([page])
    indexer = _indexer(source, store)
    await indexer.reindex()
    assert store.written  # first pass indexed it

    # Second pass: same revision, so nothing is re-embedded.
    store.written.clear()
    await indexer.reindex()
    assert store.written == []
    assert [s.path for s in indexer.skills.phrase_skills()] == ["wiki/skills/movie.md"]


async def test_a_convention_page_writes_no_trigger_row() -> None:
    """A convention is reference material, indexed as an ordinary wiki page.

    Inventing a trigger for a page of guidance is what put fragments such as
    "turn on the" into the corpus.
    """
    source = FakeSource([_page("wiki/skills/rules.md", {"name": "rules", "kind": "convention"})])
    store = FakeStore()
    indexer = _indexer(source, store)
    await indexer.reindex()
    (write,) = store.written
    assert write["kind"] != "skill"


async def test_a_command_page_writes_one_row_per_trigger() -> None:
    source = FakeSource(
        [_page("wiki/skills/movie.md", {"name": "movie", "triggers": "[movie time, film night]"})]
    )
    store = FakeStore()
    indexer = _indexer(source, store)
    await indexer.reindex()
    (write,) = store.written
    assert write["kind"] == "skill"
    assert [heading for heading, _text, _vec in write["chunks"]] == ["movie time", "film night"]
