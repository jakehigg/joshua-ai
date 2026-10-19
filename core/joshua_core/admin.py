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
- ``GET /admin/journal/status`` — today's journal: entries written, whether the
  day page exists, and the summary of the last nightly run.
- ``POST /admin/turn`` — run one turn as an operator and return the text. The
  reply is never delivered to a channel.
- ``POST /admin/jobs/{job}/run`` — start a job run in the background (body
  ``{args?, dry_run?}``; ``dry_run`` defaults to true). 202 with the run id,
  404 for an unknown job, 400 for bad arguments, 409 while a run or the
  nightly reflection holds the lock.
- ``GET /admin/jobs/{job}/status`` — whether a run holds the lock, and the
  last run of that job.
- ``GET /admin/jobs/runs/{id}`` — one run and its report.
- ``GET /admin/jobs/runs?job=&limit=`` — the newest runs, of one job or all.

Every ``/admin/jobs/*`` route answers 404 when ``jobs.enabled`` is false.
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
from joshua_core.jobs import JobArgsError, JobBusy, UnknownJob
from joshua_core.memory import embed

logger = get_logger("core.admin")


# Transcript tail bounds for ``GET /admin/transcript``.
_DEFAULT_TRANSCRIPT_LIMIT = 40
_MAX_TRANSCRIPT_LIMIT = 500

# Bounds for ``GET /admin/jobs/runs``.
_DEFAULT_RUNS_LIMIT = 20
_MAX_RUNS_LIMIT = 200

_JOBS_OFF = {"error": "jobs are off; set jobs.enabled: true in joshua.yaml"}


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


def _parse_runs_limit(raw: str | None) -> int:
    """Clamp the ``limit`` query param of the runs list to ``[1, _MAX_RUNS_LIMIT]``."""
    try:
        value = int(raw) if raw is not None else _DEFAULT_RUNS_LIMIT
    except ValueError:
        value = _DEFAULT_RUNS_LIMIT
    return max(1, min(value, _MAX_RUNS_LIMIT))


def _job_runner(request: Request) -> Any:
    """The job runner, or None when ``jobs.enabled`` is false or core has none."""
    ctx = request.app.state.ctx
    if not ctx.settings.jobs.enabled:
        return None
    return getattr(ctx, "jobs", None)


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

    @router.get("/admin/journal/status")
    async def journal_status(request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        return JSONResponse(request.app.state.ctx.reflector.journal_status())

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

    # The runs routes come before ``/admin/jobs/{job}/...`` so that ``runs`` is
    # never read as a job name.
    @router.get("/admin/jobs/runs")
    async def jobs_runs(request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        runner = _job_runner(request)
        if runner is None:
            return JSONResponse(_JOBS_OFF, status_code=404)
        job = request.query_params.get("job") or None
        limit = _parse_runs_limit(request.query_params.get("limit"))
        return JSONResponse({"runs": await runner.list_runs(job, limit)})

    @router.get("/admin/jobs/runs/{run_id}")
    async def jobs_run(run_id: str, request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        runner = _job_runner(request)
        if runner is None:
            return JSONResponse(_JOBS_OFF, status_code=404)
        try:
            number = int(run_id)
        except ValueError:
            return JSONResponse({"error": "run id must be a number"}, status_code=400)
        run = await runner.get_run(number)
        if run is None:
            return JSONResponse({"error": f"no run {number}"}, status_code=404)
        return JSONResponse(run)

    @router.post("/admin/jobs/{job}/run")
    async def jobs_start(job: str, request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        runner = _job_runner(request)
        if runner is None:
            return JSONResponse(_JOBS_OFF, status_code=404)
        raw = await request.body()
        try:
            body = await request.json() if raw.strip() else {}
        except Exception:  # noqa: BLE001 — a bad body is a 400, not a 500
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        args = body.get("args", {})
        dry_run = body.get("dry_run", True)
        if not isinstance(args, dict):
            return JSONResponse({"error": "args must be a JSON object"}, status_code=400)
        if not isinstance(dry_run, bool):
            return JSONResponse({"error": "dry_run must be true or false"}, status_code=400)
        try:
            run_id = await runner.start(job, args, dry_run)
        except UnknownJob:
            return JSONResponse({"error": f"unknown job {job}"}, status_code=404)
        except JobArgsError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except JobBusy:
            return JSONResponse(
                {"error": "a job or the nightly reflection is running; try again later"},
                status_code=409,
            )
        return JSONResponse({"run_id": run_id, "job": job, "dry_run": dry_run}, status_code=202)

    @router.get("/admin/jobs/{job}/status")
    async def jobs_status(job: str, request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        runner = _job_runner(request)
        if runner is None:
            return JSONResponse(_JOBS_OFF, status_code=404)
        if not runner.has(job):
            return JSONResponse({"error": f"unknown job {job}"}, status_code=404)
        return JSONResponse(
            {"job": job, "busy": runner.busy, "last_run": await runner.last_run(job)}
        )

    return router
