"""``POST /v1/history/search`` and ``POST /v1/history/read``: the chat history
of a worker, scoped to who started it.

The assignments container calls these two routes for the ``history`` tools of
a worker: the ``history`` module of that container. A worker runs in its own
container and has no database, so core answers the two reads and keeps the
transcript where it is.

**This is where the scope is applied.** The caller names who started the run,
never a conversation: ``person``, and ``workspace`` when the run started in a
workspace thread. :func:`scope_of` turns the two into the conversation ids of
the read, and the store query takes those ids only
(``Repo.search_transcript``, ``Repo.read_transcript``), so a row of another
conversation is never returned:

- a run that a person started reads the conversations of that person
  (``Repo.person_conversation_ids``), their personal workspace threads
  included;
- a run that a shared workspace started reads the thread of that workspace
  and nothing else. A shared thread belongs to several people and to no one
  of them, so it is never in a person's scope, and a person's other
  conversations are never in a shared workspace's scope;
- an unknown person, or a workspace that the person does not reach, has an
  empty scope, and the answer holds no message.

Both routes are read-only, create no conversation, and take the fleet bearer
of the ``assignments`` container. They return the visible rows of the
transcript as they are, so a report may quote a turn verbatim. The query and
the messages are never logged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from joshua_shared.contracts import (
    ASSIGNMENTS_IDENTITY,
    HISTORY_READ_PATH,
    HISTORY_SEARCH_PATH,
    HistoryMessage,
    HistoryReadRequest,
    HistoryReadResponse,
    HistorySearchRequest,
    HistorySearchResponse,
)
from joshua_shared.fleet_auth import require_identity
from pydantic import ValidationError

from joshua_core.engine.tools.history import READ_MAX_CHARS, READ_MAX_ROWS
from joshua_core.store.transcript import TranscriptHit, TranscriptRow

# The fleet identities that may read the history of a worker. The assignments
# container is the one worker host of this repository.
ALLOWED_CALLERS = (ASSIGNMENTS_IDENTITY,)


async def scope_of(repo: Any, person_id: str, workspace_ref: str | None) -> list[str]:
    """The conversations that a run may read: the scope of who started it.

    A run of a shared workspace gets the thread of that workspace alone. Every
    other run, a run of a personal workspace included, gets the conversations
    of its person; the thread of a personal workspace is one of them. A
    workspace that the person does not reach, and a person with no
    conversation, give an empty list.
    """
    if workspace_ref:
        workspace = await repo.find_workspace_for(person_id, workspace_ref)
        if workspace is None:
            return []
        if workspace.shared:
            # One thread of several people: never widened to a person's own
            # conversations. The route creates no conversation.
            conversation = await repo.find_conversation(workspace.channel_id, None)
            return [str(conversation.id)] if conversation is not None else []
    return await repo.person_conversation_ids(person_id)


def _iso_utc(value: Any) -> str:
    if not isinstance(value, datetime):
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def hit_json(hit: TranscriptHit) -> HistoryMessage:
    """One search match as a message of the answer."""
    return HistoryMessage(
        at=_iso_utc(hit.created_at),
        direction=hit.direction,
        speaker=hit.speaker,
        text=hit.snippet,
    )


def row_json(row: TranscriptRow) -> HistoryMessage:
    """One read message of the answer."""
    return HistoryMessage(
        at=_iso_utc(row.created_at),
        direction=row.direction,
        speaker=row.speaker,
        text=row.content,
        cut=row.cut,
    )


def _invalid(exc: ValidationError) -> JSONResponse:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first.get("loc", ())) or "body"
    return JSONResponse({"error": f"{where}: {first.get('msg', 'not valid')}"}, status_code=400)


def build_history_router() -> APIRouter:
    """The two history routes. Only the ``assignments`` container may call them."""
    router = APIRouter()

    @router.post(HISTORY_SEARCH_PATH)
    async def search_history(  # type: ignore[no-untyped-def]
        request: Request,
        _identity: str = Depends(require_identity(list(ALLOWED_CALLERS))),
    ):
        try:
            body = HistorySearchRequest.model_validate_json(await request.body())
        except ValidationError as exc:
            return _invalid(exc)
        repo = request.app.state.ctx.repo
        ids = await scope_of(repo, body.person, body.workspace)
        hits = await repo.search_transcript(ids, body.query, limit=body.limit)
        answer = HistorySearchResponse(messages=[hit_json(hit) for hit in hits])
        return JSONResponse(answer.model_dump(mode="json"))

    @router.post(HISTORY_READ_PATH)
    async def read_history(  # type: ignore[no-untyped-def]
        request: Request,
        _identity: str = Depends(require_identity(list(ALLOWED_CALLERS))),
    ):
        try:
            body = HistoryReadRequest.model_validate_json(await request.body())
        except ValidationError as exc:
            return _invalid(exc)
        start = body.start if body.start.tzinfo else body.start.replace(tzinfo=UTC)
        end = body.end or datetime.now(UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        if end <= start:
            return JSONResponse({"error": "end must be after start"}, status_code=400)
        repo = request.app.state.ctx.repo
        ids = await scope_of(repo, body.person, body.workspace)
        try:
            page = await repo.read_transcript(
                ids,
                start,
                end,
                cursor=body.cursor,
                max_rows=READ_MAX_ROWS,
                max_chars=READ_MAX_CHARS,
            )
        except ValueError:
            return JSONResponse({"error": "the cursor is not valid"}, status_code=400)
        answer = HistoryReadResponse(
            messages=[row_json(row) for row in page.rows], cursor=page.cursor
        )
        return JSONResponse(answer.model_dump(mode="json"))

    return router
