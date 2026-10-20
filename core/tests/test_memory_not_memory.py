"""A job report and a list page are not memory: the index leaves them out."""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from joshua_core.jobs.insights.page import write_index as write_insights_index
from joshua_core.jobs.research.dossier import (
    Dossier,
    Question,
    save_dossier,
)
from joshua_core.jobs.research.dossier import (
    write_index as write_knowledge_index,
)
from joshua_core.jobs.research.questions import write_index as write_questions_index
from joshua_core.memory import embed as embed_module
from joshua_core.memory.indexer import Indexer
from joshua_core.memory.models import KbChunk
from joshua_core.memory.search import search
from joshua_core.memory.sources.files import FilesSource, not_memory
from joshua_shared import frontmatter, layout

DAY = date(2026, 10, 19)
NOW = datetime(2026, 10, 19, 18, 0, tzinfo=UTC)
TOPIC = "houseplants/snake-plants-living-room-window.md"
QUESTION = "When should I fertilize the snake plant, and should I fertilize in the fall?"
KNOWLEDGE_SLUG = "snake-plants-receive-liquid-fertilizer-early"

REPORT = "wiki/journal/2026/10/19/research-20261019-6.md"
ENTRY = "wiki/journal/2026/10/19/bread.md"
HIDDEN = "wiki/notes/scratch.md"
KNOWLEDGE = f"wiki/knowledge/houseplants/snake-plants-living-room-window/{KNOWLEDGE_SLUG}.md"
DOSSIER = "wiki/knowledge/houseplants/snake-plants-living-room-window.md"

_WORD = re.compile(r"[a-z]+")
_DIMS = 97


def word_vector(text: str) -> list[float]:
    """A bag of words: one dimension for each word, by a stable hash."""
    vec = [0.0] * _DIMS
    for word in _WORD.findall(text.lower()):
        vec[sum(ord(c) * (i + 1) for i, c in enumerate(word)) % _DIMS] += 1.0
    vec[0] += 0.01
    return vec


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class MemoryKb:
    """The `kb_chunk` table in memory, with a cosine search."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str | None, str], dict[str, Any]] = {}
        self.deleted: list[str] = []

    async def kb_index_state(self, source: str) -> dict[tuple[str | None, str], str]:
        return {(p, path): row["rev"] for (s, p, path), row in self.rows.items() if s == source}

    async def kb_replace_item(
        self, *, source, person_id, path, kind, title, file_sha256, chunks, **_
    ):
        items = list(chunks)
        self.rows[(source, person_id, path)] = {
            "rev": file_sha256,
            "kind": kind,
            "title": title,
            "chunks": items,
        }
        return len(items)

    async def kb_delete_item(self, source: str, person_id: str | None, path: str) -> int:
        self.deleted.append(path)
        return 1 if self.rows.pop((source, person_id, path), None) else 0

    async def kb_purge_sources(self, keep: list[str]) -> int:
        return 0

    async def kb_search(
        self, person_id, embedding, *, k, min_sim, sources=None, kinds=None, exclude_kinds=None
    ):
        found: list[KbChunk] = []
        for (source, person, path), row in self.rows.items():
            if exclude_kinds and row["kind"] in exclude_kinds:
                continue
            for index, (heading, text, vec) in enumerate(row["chunks"]):
                sim = _cosine(embedding, vec)
                if sim >= min_sim:
                    found.append(
                        KbChunk(
                            person_id=person,
                            source=source,
                            kind=row["kind"],
                            path=path,
                            title=row["title"],
                            heading=heading,
                            chunk_index=index,
                            text=text,
                            similarity=sim,
                        )
                    )
        found.sort(key=lambda c: -(c.similarity or 0.0))
        return found[:k]


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _report(run_id: int) -> str:
    """A journal entry as the job runner writes it."""
    meta = {"date": DAY, "people": [], "source": "research", "run_id": run_id}
    block = yaml.safe_dump(meta, sort_keys=False)
    return (
        f"---\n{block}---\n\n# research run\n\nResearched one topic.\n\n"
        f"## Questions\n\n- {QUESTION}\n- Should I fertilize the snake plant in the fall?\n"
    )


def _dossier() -> Dossier:
    return Dossier(
        topic=TOPIC,
        title="Snake plants by the living room window",
        questions=[
            Question(
                slug=KNOWLEDGE_SLUG,
                question=QUESTION,
                status="answered",
                page=f"houseplants/snake-plants-living-room-window/{KNOWLEDGE_SLUG}.md",
            )
        ],
    )


def _tree(root: Path, reports: int = 1) -> None:
    for run_id in range(6, 6 + reports):
        _write(root, f"wiki/journal/2026/10/19/research-20261019-{run_id}.md", _report(run_id))
    _write(
        root,
        ENTRY,
        "---\ndate: 2026-10-19\npeople: [alex]\nsource: agent\n---\n"
        "# Bread\n\nAlex baked a sourdough loaf with a long cold rise.\n",
    )
    _write(root, HIDDEN, "---\nindex: false\n---\n# Scratch\n\nA list of things to look up.\n")
    _write(
        root,
        KNOWLEDGE,
        f"---\nquestion: {QUESTION}\ntopic: {TOPIC}\n---\n"
        "# Snake plants receive liquid fertilizer early\n\n## Answer\n\n"
        "Fertilize the snake plant in spring and summer, once a month, at half "
        "strength. Do not fertilize in the fall or in winter.\n",
    )
    save_dossier(_dossier(), root, {})


async def _listed(root: Path) -> tuple[list[str], list[str]]:
    source = FilesSource(root)
    uris = [d.uri async for d in source.list_documents()]
    return uris, source.left_out()


def _indexer(kb: MemoryKb, root: Path) -> Indexer:
    return Indexer(kb, {"files": FilesSource(root)}, embed_model="m", chunk_chars=1600)  # type: ignore[arg-type]


async def test_the_listing_leaves_out_a_report_and_a_page_that_asks_to_be_left_out(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    uris, left_out = await _listed(tmp_path)
    assert sorted(uris) == sorted([ENTRY, KNOWLEDGE])
    assert sorted(left_out) == sorted([REPORT, HIDDEN, DOSSIER])
    # The pages stay on the disk.
    assert all((tmp_path / rel).is_file() for rel in left_out)


async def test_a_page_left_out_is_not_fetched(tmp_path: Path) -> None:
    _tree(tmp_path)
    source = FilesSource(tmp_path)
    assert await source.fetch(REPORT) is None
    assert await source.fetch(HIDDEN) is None
    fetched = await source.fetch(KNOWLEDGE)
    assert fetched is not None and fetched.uri == KNOWLEDGE


def test_not_memory_reads_only_the_top_level_keys() -> None:
    nested = "---\ntopic: a.md\nquestions:\n  - slug: x\n    index: false\n---\n# A\n"
    assert not not_memory("wiki/knowledge/a.md", nested)
    assert not_memory("wiki/a.md", "---\nindex: False\n---\n# A\n")
    assert not_memory("wiki/a.md", "---\nindex: 'false'\n---\n# A\n")
    assert not not_memory("wiki/a.md", "---\nindex: true\n---\n# A\n")
    assert not not_memory("wiki/a.md", "# A\n\nindex: false\n")
    # A `run_id` marks a job report only in the journal.
    assert not_memory("wiki/journal/2026/10/19/x.md", "---\nrun_id: 3\n---\n# X\n")
    assert not not_memory("wiki/notes/x.md", "---\nrun_id: 3\n---\n# X\n")
    assert not not_memory("wiki/journal/2026/10/19/x.md", "---\nrun_id:\n---\n# X\n")


async def test_the_status_counts_the_pages_left_out(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": word_vector(text))
    _tree(tmp_path)
    indexer = _indexer(MemoryKb(), tmp_path)
    stats = (await indexer.reindex())["files"]
    assert stats["listed"] == 2 and stats["not_memory"] == 3
    status = indexer.status()["files"]["not_memory"]
    assert status == {"count": 3, "paths": sorted([REPORT, HIDDEN, DOSSIER])}


async def test_a_row_of_a_report_indexed_before_is_purged(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": word_vector(text))
    _tree(tmp_path)
    kb = MemoryKb()
    vec = word_vector(QUESTION)
    for path in (REPORT, DOSSIER):
        kb.rows[("files", None, path)] = {
            "rev": "old",
            "kind": "journal" if path == REPORT else "knowledge",
            "title": "old",
            "chunks": [("Questions", QUESTION, vec)],
        }
    stats = (await _indexer(kb, tmp_path).reindex())["files"]
    assert stats["removed"] == 2
    assert sorted(kb.deleted) == sorted([REPORT, DOSSIER])
    assert {path for _, _, path in kb.rows} == {ENTRY, KNOWLEDGE}


async def test_a_question_finds_the_knowledge_page_and_no_report(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": word_vector(text))
    _tree(tmp_path, reports=3)
    kb = MemoryKb()
    await _indexer(kb, tmp_path).reindex()
    found = await search(
        kb,  # type: ignore[arg-type]
        None,
        word_vector(QUESTION),
        k=6,
        min_sim=0.0,
        exclude_kinds=["skill"],
        prefer_recent=True,
        now=NOW,
    )
    assert found and found[0].path == KNOWLEDGE
    assert found[0].kind == "knowledge"
    paths = {chunk.path for chunk in found}
    assert not any("/research-20261019-" in path for path in paths)
    assert DOSSIER not in paths


def test_the_dossier_and_the_three_lists_hold_index_false(tmp_path: Path) -> None:
    written = [
        save_dossier(_dossier(), tmp_path, {}),
        write_knowledge_index(tmp_path),
        write_questions_index(tmp_path),
        write_insights_index(tmp_path),
    ]
    assert written[1:] == [
        layout.knowledge_index(tmp_path),
        layout.questions_index(tmp_path),
        layout.insights_index(tmp_path),
    ]
    for path in written:
        meta, _ = frontmatter.split(path.read_text())
        assert meta["index"] is False, path
