"""Person-scoped memory search: vector recall + recency-aware ranking.

``search`` over-fetches candidates by cosine from the person's scope, then
applies :func:`rank_chunks` (per-document cap and a recency bonus for ``blog``
kinds) to return the top ``k`` chunks.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from joshua_core.memory.models import KbChunk
from joshua_core.memory.ranking import PER_DOC_CAP, rank_chunks
from joshua_core.memory.store import MemoryStore

# Blog posts are time-anchored; wiki and shared reference pages are not.
TEMPORAL_KINDS: tuple[str, ...] = ("blog",)
# Fetch this many times ``k`` candidates so the per-doc cap and recency
# re-ordering have room to work before the final trim to ``k``.
_CANDIDATE_MULTIPLIER = 5


async def search(
    store: MemoryStore,
    person_id: str | None,
    query_vec: list[float],
    *,
    k: int,
    min_sim: float,
    sources: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
    exclude_kinds: Sequence[str] | None = None,
    prefer_recent: bool = False,
    recency_bonus: float = 0.08,
    recency_half_life_days: float = 14.0,
    per_doc_cap: int = PER_DOC_CAP,
    now: datetime | None = None,
) -> list[KbChunk]:
    """Return the top ``k`` chunks visible to ``person_id`` (own + shared)."""
    candidates = await store.kb_search(
        person_id,
        query_vec,
        k=max(k * _CANDIDATE_MULTIPLIER, 20),
        min_sim=min_sim,
        sources=sources,
        kinds=kinds,
        exclude_kinds=exclude_kinds,
    )
    ranked = rank_chunks(
        candidates,
        limit=k,
        min_sim=min_sim,
        now=now or datetime.now(UTC),
        temporal_kinds=TEMPORAL_KINDS,
        bonus=recency_bonus,
        half_life_days=recency_half_life_days,
        prefer_recent=prefer_recent,
        per_doc_cap=per_doc_cap,
    )
    return [chunk for _, chunk in ranked]
