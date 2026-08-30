"""Integration test for taught skills against a real pgvector database.

A real skill file in a temporary volume, one index pass, and a matching turn.
Embedding is keyword-based: a ``movie`` text and the movie triggers embed to one
unit vector, every other text to an orthogonal one, so the ``min_sim`` floor
decides a match without a downloaded model.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from joshua_core.engine.taught_skills import taught_skill_context
from joshua_core.memory import embed as embed_module
from joshua_core.memory.indexer import Indexer
from joshua_core.memory.search import search
from joshua_core.memory.skills import SKILL_KIND
from joshua_core.memory.sources.files import FilesSource
from joshua_core.memory.store import MemoryStore

pytestmark = pytest.mark.integration

VEC_MOVIE = [1.0] + [0.0] * 383
VEC_OTHER = [0.0, 1.0] + [0.0] * 382


@pytest.fixture
def keyword_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_embed(text: str, model: str = "") -> list[float]:
        return list(VEC_MOVIE) if "movie" in text.lower() else list(VEC_OTHER)

    monkeypatch.setattr(embed_module, "embed", fake_embed)


def _tree(root: Path) -> None:
    (root / "wiki/skills").mkdir(parents=True)
    (root / "wiki/skills/movie.md").write_text(
        "---\n"
        "name: movie time\n"
        'triggers: ["movie time", "let\'s watch a movie"]\n'
        "---\n"
        "Dim the living room lights to 30 percent and turn on the TV.\n"
    )
    (root / "wiki/notes.md").write_text(
        "# Notes\n\nAn ordinary wiki page about movie night ideas.\n"
    )


async def _indexed(pool, root: Path) -> MemoryStore:
    store = MemoryStore(pool)
    indexer = Indexer(store, {"files": FilesSource(root)}, embed_model="m", chunk_chars=1600)
    await indexer.reindex()
    return store


async def _ctx(repo, text: str, person_id: str | None) -> SimpleNamespace:
    await repo.upsert_channel("telegram:x", "telegram", default_person_id=person_id)
    conv = await repo.get_or_create_conversation("telegram:x", person_id)
    return SimpleNamespace(
        text=text,
        person_id=person_id,
        conversation=SimpleNamespace(id=conv.id),
        turn_id="t-x",
        embedding=None,
    )


async def _run(ctx: Any, store: MemoryStore, repo) -> str | None:
    return await taught_skill_context(
        ctx, store=store, repo=repo, embed_model="m", min_sim=0.62, top_k=2
    )


async def test_one_row_per_trigger_with_skill_kind(db, repo, tmp_path: Path, keyword_embed) -> None:
    _tree(tmp_path)
    store = await _indexed(db.pool, tmp_path)

    rows = await store.kb_search(None, VEC_MOVIE, k=10, min_sim=0.0, kinds=(SKILL_KIND,))
    assert len(rows) == 2  # one row per trigger phrase
    assert {r.heading for r in rows} == {"movie time", "let's watch a movie"}
    assert all(r.kind == "skill" for r in rows)
    assert all("Dim the living room" in r.text for r in rows)  # each row holds the instructions


async def test_matching_turn_fires_and_weather_does_not(
    db, repo, tmp_path: Path, keyword_embed
) -> None:
    await repo.upsert_person("alice", "Alice")
    _tree(tmp_path)
    store = await _indexed(db.pool, tmp_path)

    hit = await _run(await _ctx(repo, "let's watch a movie", "alice"), store, repo)
    assert hit is not None
    assert hit.startswith('[Taught skill "movie time" matches this request')
    assert "Dim the living room lights" in hit

    miss = await _run(await _ctx(repo, "what is the weather today", "alice"), store, repo)
    assert miss is None


async def test_guest_turn_fires_the_shared_skill(db, repo, tmp_path: Path, keyword_embed) -> None:
    _tree(tmp_path)
    store = await _indexed(db.pool, tmp_path)

    # A group/guest turn (person_id None) still sees the shared wiki skill.
    fired = await _run(await _ctx(repo, "let's watch a movie", None), store, repo)
    assert fired is not None
    assert "Dim the living room lights" in fired


async def test_skill_rows_excluded_from_ordinary_recall(
    db, repo, tmp_path: Path, keyword_embed
) -> None:
    _tree(tmp_path)
    store = await _indexed(db.pool, tmp_path)

    recall = await search(store, None, VEC_MOVIE, k=10, min_sim=0.0, exclude_kinds=(SKILL_KIND,))
    paths = {c.path for c in recall}
    assert "wiki/notes.md" in paths  # an ordinary page still comes back
    assert "wiki/skills/movie.md" not in paths  # a trigger row never does
