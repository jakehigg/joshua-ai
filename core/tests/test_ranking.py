"""Ranking tests, ported from joshua-kb and adapted to the kernel model.

The kernel drops the enrichment chunk types, so ranking keeps only the raw
per-document cap and the recency bonus. Recency keys off the document ``kind``
(``journal`` is temporal; ``wiki``/``shared`` are static) and the ``doc_date``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from joshua_core.memory.models import KbChunk
from joshua_core.memory.ranking import rank_chunks

NOW = datetime(2026, 7, 3, 18, 0, tzinfo=UTC)
KW = dict(min_sim=0.55, now=NOW, temporal_kinds=("journal",), bonus=0.08, half_life_days=14.0)


def _chunk(kind: str, path: str, idx: int, sim: float, age_days: int | None = None) -> KbChunk:
    doc_date = (NOW.date() - timedelta(days=age_days)) if age_days is not None else None
    return KbChunk(
        person_id="alice",
        source="files",
        kind=kind,
        path=path,
        chunk_index=idx,
        similarity=sim,
        doc_date=doc_date,
    )


def test_prefer_recent_lifts_fresh_journal() -> None:
    pool = [
        _chunk("wiki", "wiki/p1.md", 0, 0.79),
        _chunk("journal", "journal/m1.md", 0, 0.70, age_days=0),
    ]
    top = rank_chunks(pool, limit=2, prefer_recent=False, **KW)
    assert top[0][1].kind == "wiki"
    top = rank_chunks(pool, limit=2, prefer_recent=True, **KW)
    assert top[0][1].kind == "journal"


def test_stale_journal_competes_on_similarity() -> None:
    pool = [
        _chunk("wiki", "wiki/p1.md", 0, 0.72),
        _chunk("journal", "journal/old.md", 0, 0.70, age_days=90),
    ]
    top = rank_chunks(pool, limit=2, prefer_recent=True, **KW)
    assert top[0][1].kind == "wiki"


def test_floor_applies_to_raw_similarity() -> None:
    pool = [_chunk("journal", "journal/fresh.md", 0, 0.50, age_days=0)]
    assert rank_chunks(pool, limit=5, prefer_recent=True, **KW) == []


def test_per_document_cap() -> None:
    pool = [_chunk("wiki", "wiki/big.md", i, 0.80 - i * 0.01) for i in range(4)]
    pool.append(_chunk("journal", "journal/m1.md", 0, 0.60, age_days=1))
    top = rank_chunks(pool, limit=3, prefer_recent=False, per_doc_cap=2, **KW)
    assert [c.path for _, c in top] == ["wiki/big.md", "wiki/big.md", "journal/m1.md"]


def test_static_kinds_never_boosted() -> None:
    pool = [
        _chunk("wiki", "wiki/fresh.md", 0, 0.70, age_days=0),
        _chunk("wiki", "wiki/old.md", 0, 0.71, age_days=400),
    ]
    top = rank_chunks(pool, limit=2, prefer_recent=True, **KW)
    assert top[0][1].path == "wiki/old.md"


def test_shared_and_own_share_the_slots() -> None:
    # A shared document (person_id None) and an own document are distinct docs and
    # both count against the cap independently.
    own = KbChunk(
        person_id="alice",
        source="files",
        kind="wiki",
        path="wiki/x.md",
        chunk_index=0,
        similarity=0.80,
        doc_date=None,
    )
    shared = KbChunk(
        person_id=None,
        source="files",
        kind="shared",
        path="shared/x.md",
        chunk_index=0,
        similarity=0.78,
        doc_date=None,
    )
    top = rank_chunks([own, shared], limit=5, prefer_recent=False, per_doc_cap=1, **KW)
    assert {c.path for _, c in top} == {"wiki/x.md", "shared/x.md"}


def test_recency_bonus_uses_doc_date() -> None:
    # A journal page dated today outranks one dated a year ago at equal similarity.
    old = KbChunk(
        person_id="alice",
        source="files",
        kind="journal",
        path="journal/old.md",
        chunk_index=0,
        similarity=0.70,
        doc_date=date(2025, 7, 3),
    )
    new = KbChunk(
        person_id="alice",
        source="files",
        kind="journal",
        path="journal/new.md",
        chunk_index=0,
        similarity=0.70,
        doc_date=NOW.date(),
    )
    top = rank_chunks([old, new], limit=2, prefer_recent=False, **KW)
    assert top[0][1].path == "journal/new.md"
