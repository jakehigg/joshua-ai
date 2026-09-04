"""Retrieval ranking — blends cosine similarity with kind temporality.

Wiki pages are static reference: their age says nothing about relevance.
Journal pages (the nightly page and the entries Joshua writes during the day)
are temporal: for "where do we stand" questions, a fresh page beats a
slightly-better-matching page from last month. So temporal kinds get a recency
bonus that DECAYS (half-life, default 2 weeks) — a fresh page is lifted past
static content, a stale page competes on similarity alone. The caller signals
time-intent per query via ``prefer_recent``, which doubles the bonus.

Also enforces a per-document cap so one strong document can't monopolize the
result slots and crowd out every other document.

Pure functions — no DB, no embedding — so this stays offline-testable and the
tuning knobs are auditable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from joshua_core.memory.models import KbChunk

# Max chunks one document may contribute to a result set, so one page can't fill
# every slot. Overridden per call by ``memory.per_doc_cap``.
PER_DOC_CAP = 3


def recency_bonus(
    chunk: KbChunk,
    *,
    now: datetime,
    temporal_kinds: Sequence[str],
    bonus: float,
    half_life_days: float,
    prefer_recent: bool,
) -> float:
    """Additive score bonus for fresh chunks of temporal kinds; 0 otherwise."""
    if chunk.kind not in temporal_kinds or chunk.doc_date is None:
        return 0.0
    age_days = max(0.0, float((now.date() - chunk.doc_date).days))
    scale = 2.0 if prefer_recent else 1.0
    return bonus * scale * 0.5 ** (age_days / max(half_life_days, 0.01))


def rank_chunks(
    chunks: Iterable[KbChunk],
    *,
    limit: int,
    min_sim: float,
    now: datetime,
    temporal_kinds: Sequence[str],
    bonus: float,
    half_life_days: float,
    prefer_recent: bool,
    per_doc_cap: int = PER_DOC_CAP,
) -> list[tuple[float, KbChunk]]:
    """Blend similarity + recency, cap chunks per document, return the top
    ``limit`` as ``(blended_score, chunk)``. The floor applies to the RAW
    similarity — recency can reorder genuine matches but never lift an off-topic
    chunk over the relevance bar.
    """
    scored: list[tuple[float, KbChunk]] = []
    for c in chunks:
        sim = c.similarity or 0.0
        if sim < min_sim:
            continue
        b = recency_bonus(
            c,
            now=now,
            temporal_kinds=temporal_kinds,
            bonus=bonus,
            half_life_days=half_life_days,
            prefer_recent=prefer_recent,
        )
        scored.append((sim + b, c))
    scored.sort(key=lambda t: (-t[0], t[1].source, t[1].path, t[1].chunk_index))

    out: list[tuple[float, KbChunk]] = []
    counts: dict[tuple[str | None, str, str], int] = {}
    for score, c in scored:
        key = (c.person_id, c.source, c.path)
        if counts.get(key, 0) >= per_doc_cap:
            continue
        counts[key] = counts.get(key, 0) + 1
        out.append((score, c))
        if len(out) >= limit:
            break
    return out
