"""Core admin routes for people and operability.

Every ``/admin/*`` route requires an identity in ``ADMIN_CALLERS`` (env, default
``laptop,ci``) — checked on every route with no exception. A missing or invalid
bearer is 401; a valid identity outside the allowlist is 403.

Routes:

- ``GET /admin/people`` / ``POST /admin/people`` — read and extend the roster.
- ``GET /admin/pool`` — the warm session pool.
- ``POST /admin/sessions/flush`` — drop every pooled session so the next turn
  reconnects with the current gateway toolset.
- ``GET /admin/transcript/{conversation_id}`` — the last rows of a conversation.
- ``POST /admin/kb/reindex`` — reconcile the memory index (body ``{source?,
  person?, full?, background?}``). ``background`` answers 202 at once and runs
  the pass after, for a caller that must not wait.
- ``GET /admin/kb/status`` — per-source indexer state and chunk counts.
- ``GET /admin/kb/events`` — recent retrieval-audit rows (body ``{limit?}``).
- ``POST /admin/reflect`` — run or re-run the nightly reflection for one day
  (body ``{person?, date?}``); re-running a date overwrites that day's post.
- ``POST /admin/turn`` — run one turn as an operator and return the text. The
  reply is never delivered to a channel.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from joshua_shared.fleet_auth import (
    DEFAULT_ADMIN_CALLERS,
    authenticate,
    load_fleet_tokens,
    parse_callers,
)
from joshua_shared.log import get_logger

from joshua_core import people
from joshua_core.memory import embed

logger = get_logger("core.admin")


# Transcript tail bounds for ``GET /admin/transcript``.
_DEFAULT_TRANSCRIPT_LIMIT = 40
_MAX_TRANSCRIPT_LIMIT = 500


def _log_background_reindex(task: asyncio.Task[Any]) -> None:
    """Report a background pass. Nobody waits for it, so a failure that is not
    logged here is lost. ``GET /admin/kb/status`` holds the same error."""
    if task.cancelled():
        logger.warning({"message": "background reindex cancelled"})
        return
    error = task.exception()
    if error is not None:
        logger.error({"message": "background reindex failed", "error": str(error)})
    else:
        logger.info({"message": "background reindex done", "results": task.result()})


def _data_dir() -> str:
    return os.environ.get("JOSHUA_DATA_DIR", "/data")


def _parse_limit(raw: str | None) -> int:
    """Clamp the ``limit`` query param to ``[1, _MAX_TRANSCRIPT_LIMIT]``."""
    try:
        value = int(raw) if raw is not None else _DEFAULT_TRANSCRIPT_LIMIT
    except ValueError:
        value = _DEFAULT_TRANSCRIPT_LIMIT
    return max(1, min(value, _MAX_TRANSCRIPT_LIMIT))


def _json_safe(row: dict[str, Any]) -> dict[str, Any]:
    """Return a transcript row with its timestamp rendered as an ISO string."""
    created = row.get("created_at")
    if isinstance(created, datetime):
        row = {**row, "created_at": created.isoformat()}
    return row


def _json_safe_event(row: dict[str, Any]) -> dict[str, Any]:
    """Render a kb_event row for JSON: the timestamp as ISO and the UUID as text."""
    out = dict(row)
    created = out.get("created_at")
    if isinstance(created, datetime):
        out["created_at"] = created.isoformat()
    if out.get("conversation_id") is not None:
        out["conversation_id"] = str(out["conversation_id"])
    return out


def _authorize(request: Request) -> JSONResponse | None:
    """Return a 401/403 response when the request is not an admin caller, else None."""
    admin_callers = parse_callers(os.environ.get("ADMIN_CALLERS", DEFAULT_ADMIN_CALLERS))
    status, identity = authenticate(
        request.headers.get("authorization"), load_fleet_tokens(), allowed=admin_callers
    )
    if status == 401:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if status == 403:
        return JSONResponse({"error": f"caller {identity} not in ADMIN_CALLERS"}, status_code=403)
    return None


def build_admin_router() -> APIRouter:
    router = APIRouter()

    @router.get("/admin/people")
    async def get_people(request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        repo = request.app.state.ctx.repo
        return JSONResponse({"people": await people.roster(repo)})

    @router.post("/admin/people")
    async def post_people(request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a bad body is a 400, not a 500
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        repo = request.app.state.ctx.repo
        try:
            result = await people.add_person(
                repo,
                _data_dir(),
                person_id=str(body.get("id", "")),
                name=str(body.get("name", "")),
                handle=str(body.get("handle", "")),
                role=str(body.get("role") or "member"),
            )
        except people.PeopleError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"person": result}, status_code=201)

    @router.get("/admin/pool")
    async def get_pool(request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        manager = request.app.state.ctx.manager
        return JSONResponse(await manager.admin_pool())

    @router.post("/admin/sessions/flush")
    async def flush_sessions(request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        manager = request.app.state.ctx.manager
        flushed = await manager.flush_sessions()
        return JSONResponse({"flushed": flushed})

    @router.get("/admin/transcript/{conversation_id}")
    async def get_transcript(conversation_id: str, request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        repo = request.app.state.ctx.repo
        limit = _parse_limit(request.query_params.get("limit"))
        rows = await repo.transcript_tail(conversation_id, limit)
        return JSONResponse(
            {"conversation_id": conversation_id, "rows": [_json_safe(r) for r in rows]}
        )

    @router.post("/admin/kb/reindex")
    async def kb_reindex(request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a bad body is a 400, not a 500
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        indexer = request.app.state.ctx.indexer
        source = str(body["source"]) if body.get("source") else None
        person = str(body["person"]) if body.get("person") else None
        full = bool(body.get("full"))

        if bool(body.get("background")):
            # A full re-embed of a large corpus takes minutes and holds a CPU
            # for all of them. `background` starts the pass and answers at
            # once, so a caller such as the restore script does not wait.
            # `GET /admin/kb/status` reports progress and any error.
            task = asyncio.create_task(indexer.reindex(source=source, person=person, full=full))
            tasks = getattr(request.app.state, "kb_tasks", None)
            if tasks is None:
                tasks = set()
                request.app.state.kb_tasks = tasks
            # Hold a reference. asyncio keeps only a weak one, so a task with
            # no reference can be collected in the middle of the pass.
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            task.add_done_callback(_log_background_reindex)
            return JSONResponse(
                {"started": True, "source": source, "person": person, "full": full},
                status_code=202,
            )

        results = await indexer.reindex(source=source, person=person, full=full)
        return JSONResponse({"results": results})

    @router.get("/admin/kb/status")
    async def kb_status(request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        ctx = request.app.state.ctx
        return JSONResponse(
            {
                "sources": ctx.indexer.status(),
                "stats": await ctx.memory.kb_stats(),
                "embed": embed.is_available(),
            }
        )

    @router.get("/admin/kb/events")
    async def kb_events(request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        limit = _parse_limit(request.query_params.get("limit"))
        rows = await request.app.state.ctx.repo.kb_events(limit)
        return JSONResponse({"events": [_json_safe_event(r) for r in rows]})

    @router.post("/admin/reflect")
    async def post_reflect(request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a bad body is a 400, not a 500
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        target_date = None
        raw_date = body.get("date")
        if raw_date:
            try:
                target_date = datetime.strptime(str(raw_date), "%Y-%m-%d").date()
            except ValueError:
                return JSONResponse({"error": "date must be YYYY-MM-DD"}, status_code=400)
        reflector = request.app.state.ctx.reflector
        summary = await reflector.reflect(
            target_date=target_date, person=str(body["person"]) if body.get("person") else None
        )
        return JSONResponse(summary)

    @router.post("/admin/turn")
    async def post_turn(request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a bad body is a 400, not a 500
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        channel_ref = str(body.get("channel", "")).strip()
        text = str(body.get("text", ""))
        if not channel_ref or not text:
            return JSONResponse({"error": "channel and text are required"}, status_code=400)

        ctx = request.app.state.ctx
        channel = await ctx.repo.resolve_channel(channel_ref)
        if channel is None:
            return JSONResponse({"error": f"unknown channel {channel_ref}"}, status_code=404)

        person_id = str(body.get("person") or "") or channel.default_person_id
        conv_person = None if channel.session_mode == "shared" else person_id
        conversation = await ctx.repo.get_or_create_conversation(channel.id, conv_person)
        framing = body.get("framing")
        result = await ctx.manager.run_turn(
            channel,
            conversation,
            text,
            framing=str(framing) if framing else None,
            person_id=person_id,
            direction="in",
        )
        return JSONResponse({"conversation_id": conversation.id, "text": result.text or ""})

    return router
