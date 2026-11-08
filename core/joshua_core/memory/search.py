"""Person-scoped memory search: vector recall + recency-aware ranking.

``search`` over-fetches candidates by cosine from the person's scope, then
applies :func:`rank_chunks` (per-document cap and a recency bonus for
``journal`` kinds) to return the top ``k`` chunks. A chunk of a page in a
folder of ``exclude_folders`` (``memory.exclude_folders``) is never returned,
also when the index still holds it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from joshua_core.memory.models import KbChunk
from joshua_core.memory.ranking import PER_DOC_CAP, rank_chunks
from joshua_core.memory.store import MemoryStore

_WIKI_PREFIX = "wiki/"


def excluded_prefixes(folders: Sequence[str]) -> tuple[str, ...]:
    """The path prefixes of the top-level wiki folders in ``folders``, such as
    ``wiki/assignments/``."""
    return tuple(f"{_WIKI_PREFIX}{name.strip('/')}/" for name in folders if name.strip("/"))


def is_excluded(path: str, prefixes: tuple[str, ...]) -> bool:
    """True when ``path`` is in one of the folders of ``prefixes``."""
    return bool(prefixes) and path.startswith(prefixes)


# Journal pages are time-anchored; wiki and profile pages are not.
TEMPORAL_KINDS: tuple[str, ...] = ("journal",)
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
    exclude_folders: Sequence[str] = (),
) -> list[KbChunk]:
    """Return the top ``k`` chunks visible to ``person_id`` (own + shared),
    with no chunk in a folder of ``exclude_folders``."""
    prefixes = excluded_prefixes(exclude_folders)
    candidates = await store.kb_search(
        person_id,
        query_vec,
        k=max(k * _CANDIDATE_MULTIPLIER, 20),
        min_sim=min_sim,
        sources=sources,
        kinds=kinds,
        exclude_kinds=exclude_kinds,
    )
    if prefixes:
        candidates = [c for c in candidates if not is_excluded(c.path, prefixes)]
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
