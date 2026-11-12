"""Person-scoped memory search: vector recall + recency-aware ranking.

``search`` over-fetches candidates by cosine from the person's scope, then
applies :func:`rank_chunks` (per-document cap and a recency bonus for
``journal`` kinds) to return the top ``k`` chunks. A chunk of a page in a
folder of ``exclude_folders`` (``memory.exclude_folders``) is never returned,
also when the index still holds it.

A ``document`` row (the ``documents`` source) is returned only when the
search names a ``document_reader``: a member, who finds their own documents
and the shared ones. A search with no reader, such as a group turn or a turn
of a guest, finds no document.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from joshua_core.memory.models import KbChunk
from joshua_core.memory.ranking import PER_DOC_CAP, rank_chunks
from joshua_core.memory.store import DOCUMENT_KIND, WORKSPACE_KIND, MemoryStore

_WIKI_PREFIX = "wiki/"


def excluded_prefixes(folders: Sequence[str]) -> tuple[str, ...]:
    """The path prefixes of the top-level wiki folders in ``folders``, such as
    ``wiki/assignments/``."""
    return tuple(f"{_WIKI_PREFIX}{name.strip('/')}/" for name in folders if name.strip("/"))


def is_excluded(path: str, prefixes: tuple[str, ...]) -> bool:
    """True when ``path`` is in one of the folders of ``prefixes``."""
    return bool(prefixes) and path.startswith(prefixes)


def document_reader(people: Sequence[Any], person_id: str | None) -> str | None:
    """The person whose documents a search may return: ``person_id`` when it
    names a member of ``people``, else None.

    Pass the person of the conversation, not the speaker of a turn, so a group
    conversation (no person) never shows the documents of one member.
    """
    if person_id is None:
        return None
    for person in people:
        if person.id == person_id:
            return person_id if person.role == "member" else None
    return None


def document_visible(chunk: KbChunk, reader: str | None) -> bool:
    """True when ``chunk`` is not a document, or is a document of ``reader``
    or a shared one and ``reader`` is set."""
    if chunk.kind != DOCUMENT_KIND:
        return True
    return reader is not None and chunk.person_id in (None, reader)


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
    document_reader: str | None = None,
) -> list[KbChunk]:
    """Return the top ``k`` chunks visible to ``person_id`` (own + shared),
    with no chunk in a folder of ``exclude_folders``.

    A document row is returned only for ``document_reader`` (see
    :func:`document_reader`); with no reader, the database query excludes the
    kind. The kind and the owner of each row are checked again here."""
    prefixes = excluded_prefixes(exclude_folders)
    if document_reader is None:
        exclude_kinds = tuple(dict.fromkeys([*(exclude_kinds or ()), DOCUMENT_KIND]))
    else:
        # The person rule of the database uses ``person_id``, so the reader is
        # the person of the search.
        person_id = document_reader
    if WORKSPACE_KIND not in (kinds or ()):
        # A workspace row is for the one-line hint of normal chat only.
        exclude_kinds = tuple(dict.fromkeys([*(exclude_kinds or ()), WORKSPACE_KIND]))
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
    candidates = [c for c in candidates if document_visible(c, document_reader)]
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
