"""``POST /v1/memory/search``: search the wiki, the journal, and the profiles by
meaning, for a caller outside the agent.

The viewer and the joshua-mcp addon call this route. Core owns the index and
the embedding model, so a caller sends a query and gets passages back; it never
reads the database.

The search is always in the shared scope (``person_id`` None), and the caller
names no person. It returns only the ``files`` source and only the kinds in
``SEARCH_KINDS``: every row it can return is a wiki page that any reader of the
wiki can already open. A person's own attachments (kind ``people``) and the
skill rows are never returned, and neither is a page in a folder of
``memory.exclude_folders``. The route checks the kind and the path of each
row again after the database filter, so a new kind in the indexer does not
widen what the route returns.

``source: documents`` searches the ``documents`` source in place of the
wiki: one row per document, with the title, the kind, the status, and the
summary, never the body. Such a search names ``person`` (400 when it is
missing), and returns the documents of that person and the shared ones. A
person who is not a member, or ``memory.documents.enabled: false``, gets no
results. The caller vouches for the person: the viewer sends the person who
signed in.

``memory.search.allowed_callers`` names the fleet identities that may call.
An empty list turns the route off, so every caller gets 403. The query is never
logged.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from joshua_shared.contracts import SEARCH_KINDS, SearchHit, SearchRequest, SearchResponse
from joshua_shared.fleet_auth import identify_bearer, load_fleet_tokens
from joshua_shared.log import get_logger
from pydantic import ValidationError

from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk
from joshua_core.memory.search import document_reader, excluded_prefixes, is_excluded, search
from joshua_core.memory.sources.documents import DOCUMENTS_PREFIX, DOCUMENTS_SOURCE
from joshua_core.memory.store import DOCUMENT_KIND

logger = get_logger("search_api")

SEARCH_PATH = "/v1/memory/search"

# The one source this route reads. An optional source such as ``memos`` can hold
# a row scoped to one person, and this route names no person.
_SOURCE = "files"
_WIKI_PREFIX = "wiki/"


def _authorize(request: Request) -> tuple[JSONResponse | None, str | None]:
    """Return ``(denial, None)`` or ``(None, identity)``.

    An empty ``allowed_callers`` denies every caller. That is different from
    ``fleet_auth.authenticate``, where an empty list admits any identity.
    """
    identity = identify_bearer(request.headers.get("authorization"), load_fleet_tokens())
    if identity is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401), None
    settings = request.app.state.ctx.settings
    allowed = {name.strip().lower() for name in settings.memory.search.allowed_callers}
    if identity not in allowed:
        return JSONResponse({"error": "forbidden"}, status_code=403), None
    return None, identity


def _visible(chunk: KbChunk, kinds: tuple[str, ...], excluded: tuple[str, ...] = ()) -> bool:
    """True for a row this route may return: a wiki page of a requested kind,
    not in an excluded folder. ``kinds`` is always a subset of
    ``SEARCH_KINDS``."""
    return (
        chunk.source == _SOURCE
        and chunk.kind in kinds
        and chunk.person_id is None
        and chunk.path.startswith(_WIKI_PREFIX)
        and not is_excluded(chunk.path, excluded)
    )


def _visible_document(chunk: KbChunk, reader: str) -> bool:
    """True for a document row of ``reader`` or a shared one."""
    return (
        chunk.source == DOCUMENTS_SOURCE
        and chunk.kind == DOCUMENT_KIND
        and chunk.person_id in (None, reader)
        and chunk.path.startswith(DOCUMENTS_PREFIX)
    )


def _hit(chunk: KbChunk) -> SearchHit:
    return SearchHit(
        path=chunk.path,
        title=chunk.title,
        heading=chunk.heading,
        text=chunk.text,
        kind=chunk.kind,  # type: ignore[arg-type] — _visible checked it
        score=round(chunk.similarity or 0.0, 4),
        date=chunk.doc_date.isoformat() if chunk.doc_date else None,
    )


def build_search_router() -> APIRouter:
    router = APIRouter()

    @router.post(SEARCH_PATH)
    async def memory_search(request: Request):  # type: ignore[no-untyped-def]
        # The bearer is checked before the body is read, so a caller with no
        # token learns nothing about the request shape.
        denied, identity = _authorize(request)
        if denied is not None:
            return denied
        try:
            body = SearchRequest.model_validate_json(await request.body())
        except ValidationError as exc:
            detail = exc.errors(include_url=False, include_input=False, include_context=False)
            return JSONResponse({"error": "invalid request", "detail": detail}, status_code=422)
        query = body.query.strip()
        if not query:
            return JSONResponse(SearchResponse(results=[]).model_dump())
        settings = request.app.state.ctx.settings
        memory = settings.memory
        reader: str | None = None
        if body.source == DOCUMENTS_SOURCE:
            if body.person is None:
                return JSONResponse(
                    {"error": "a search of documents needs a person"}, status_code=400
                )
            reader = document_reader(settings.people, body.person)
            if reader is None or not memory.documents.enabled:
                return JSONResponse(SearchResponse(results=[]).model_dump())
        vec = await asyncio.to_thread(embed_module.embed, query, memory.embed_model)
        if vec is None:
            return JSONResponse({"error": "embedding unavailable"}, status_code=503)
        if reader is not None:
            chunks = await search(
                request.app.state.ctx.memory,
                reader,
                vec,
                k=body.limit,
                min_sim=memory.min_sim,
                sources=(DOCUMENTS_SOURCE,),
                kinds=(DOCUMENT_KIND,),
                per_doc_cap=1,
                document_reader=reader,
            )
            hits = [_hit(c) for c in chunks if _visible_document(c, reader)]
            logger.info(
                {
                    "message": "memory search",
                    "caller": identity,
                    "source": "documents",
                    "results": len(hits),
                }
            )
            return JSONResponse(SearchResponse(results=hits).model_dump())
        kinds = tuple(dict.fromkeys(body.kinds or SEARCH_KINDS))
        chunks = await search(
            request.app.state.ctx.memory,
            None,
            vec,
            k=body.limit,
            min_sim=memory.min_sim,
            sources=(_SOURCE,),
            kinds=kinds,
            recency_bonus=memory.recency_bonus,
            recency_half_life_days=memory.recency_half_life_days,
            per_doc_cap=memory.per_doc_cap,
            exclude_folders=memory.exclude_folders,
        )
        excluded = excluded_prefixes(memory.exclude_folders)
        hits = [_hit(c) for c in chunks if _visible(c, kinds, excluded)]
        logger.info({"message": "memory search", "caller": identity, "results": len(hits)})
        return JSONResponse(SearchResponse(results=hits).model_dump())

    return router
