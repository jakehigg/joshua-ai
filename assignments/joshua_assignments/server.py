"""The assignments manager over MCP.

The container serves MCP at ``/mcp`` over streamable HTTP on port 8000.
``GET /healthz`` and ``GET /readyz`` are open and name no person. Every other
route needs ``Authorization: Bearer <token>``; a missing or wrong token gets
401. A bearer from ``ASSIGNMENTS_TOKENS`` names a person. The ``ADDON_TOKEN``
bearer names no person: it may use ``assignment_status`` and
``list_assignments`` for every assignment, and every other tool gives it
"403 forbidden". A person must be a member, and sees only their own
assignments: another person's assignment is "404 not found".
"""

from __future__ import annotations

import contextlib
import hmac
from collections.abc import AsyncIterator
from typing import Any

from joshua_shared.log import get_logger
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from joshua_assignments.manager import Manager
from joshua_assignments.settings import Caller, Settings
from joshua_assignments.store import Store

logger = get_logger("joshua_assignments")

OPEN_PATHS = frozenset({"/healthz", "/readyz"})
_BEARER = "Bearer "
CONVERSATION_HEADER = "x-joshua-conversation"
READY_COUNTS = ("queued", "running", "waiting", "paused_limits")

mcp = MCPServer(name="assignments")

_manager: Manager | None = None


def get_manager() -> Manager:
    if _manager is None:
        raise RuntimeError("build_app has not run")
    return _manager


# --- callers ---------------------------------------------------------------


def find_caller(settings: Settings, authorization: str | None) -> Caller | None:
    """The caller that ``authorization`` names, or None for a missing or wrong token.

    Every configured token is compared with ``hmac.compare_digest`` and there
    is no early exit, so the time taken does not tell which token a bearer is
    close to.
    """
    raw = authorization or ""
    provided = raw[len(_BEARER) :] if raw.startswith(_BEARER) else ""
    found = None
    for token, caller in settings.tokens.items():
        if hmac.compare_digest(provided.encode(), token.encode()) and provided:
            found = caller
    return found


def _caller(ctx: Context) -> Caller:
    # The middleware checked this header on the HTTP request that carries
    # this message, so here it only selects the caller.
    headers = ctx.headers or {}
    caller = find_caller(get_manager().settings, headers.get("authorization"))
    if caller is None:
        raise ToolError("401 unauthorized")
    if caller.person is not None and not get_manager().is_member(caller.person):
        logger.warning({"message": "refused a caller that is not a member"})
        raise ToolError("403 forbidden: assignments are for members only")
    return caller


def _person(ctx: Context) -> str:
    caller = _caller(ctx)
    if caller.person is None:
        logger.warning({"message": "refused a person tool to a caller with no person"})
        raise ToolError("403 forbidden: this tool needs a person")
    return caller.person


async def _own(caller: Caller, assignment_id: int) -> dict[str, Any]:
    """The assignment, when the caller may see it. Another person's is "not found"."""
    row = await get_manager().store.get(int(assignment_id))
    if row is None or (caller.person is not None and row["person_id"] != caller.person):
        raise ToolError(f"404 not found: no assignment {assignment_id}")
    return row


# --- tools -----------------------------------------------------------------


@mcp.tool()
async def start_assignment(
    ctx: Context, spec: dict[str, Any] | None = None, saved: str | None = None
) -> dict:
    """Start a long job that runs in the background. Returns id, status, and title at once.

    Give spec, or saved: the name of a saved assignment. With saved, a spec
    beside it replaces only the top-level fields that it gives, for this run
    (such as {"model": "opus"}).

    spec is the whole brief; the assignment sees nothing else of the chat:
    goal (required): what to do, what done means, and where each result goes.
    title: a short name, at most 120 characters; without it, the first
    sentence of the goal. model: a name from
    assignments.models; without it, the default. budget: five_hour_pct and
    seven_day_pct stop the assignment when that subscription window reaches
    the percent used; tokens caps its own tokens. A ceiling above the
    maximum is clamped, and the reply says so. max_turns: a soft cap on model
    turns. working_time: such as 4h; the clock runs only while it works.
    repos: what it may clone, each in assignments.repos. tools: the MCP
    servers it may use, from the ones you may use. tasks_project: where
    follow-ups go. report_to: conversation, or a destination name. dry_run:
    true plans and reports and changes nothing. The report and any question
    arrive later as a turn in the conversation.
    """
    person = _person(ctx)
    conversation = (ctx.headers or {}).get(CONVERSATION_HEADER)
    return await get_manager().start(person, spec, conversation, saved=saved)


@mcp.tool()
async def save_assignment(name: str, spec: dict[str, Any], ctx: Context) -> dict:
    """Save a spec with a name, to start it again later by name. Returns id, name, and notes.

    name is 2 to 60 lower case letters, digits, and hyphens, such as
    docs-cleanup. The spec is checked as start_assignment checks it. A saved
    assignment with the same name gets the new spec. Nothing runs now.
    """
    person = _person(ctx)
    return await get_manager().save(person, name, spec)


@mcp.tool()
async def list_saved_assignments(ctx: Context) -> dict:
    """The person's saved assignments, each with its name, title, runs, and last_run."""
    person = _person(ctx)
    return await get_manager().list_saved(person)


@mcp.tool()
async def delete_saved_assignment(name: str, ctx: Context) -> dict:
    """Delete one saved assignment. Its runs stay."""
    person = _person(ctx)
    return await get_manager().delete_saved(person, name)


@mcp.tool()
async def assignment_status(id: int, ctx: Context) -> dict:
    """The state of one assignment.

    status is queued, running, waiting, paused_limits, done, failed,
    cancelled, budget_exceeded, or time_exceeded. state is queued,
    in_progress, or completed. The result also has the title, the plan of
    the worker, the report and report_at, the recorded outputs, the last
    checkpoint, the usage, the turns, the open question, the limits at the
    stop, the summary, the error, and saved: the saved assignment that it
    started from, or null. Read the report here.
    """
    caller = _caller(ctx)
    return await get_manager().status(await _own(caller, id))


@mcp.tool()
async def list_assignments(
    ctx: Context, limit: int = 10, status: str | None = None, state: str | None = None
) -> dict:
    """The person's assignments, the newest first, each with its title, state, and saved.

    limit is 1 to 100. status filters by status. state filters by state:
    queued, in_progress, or completed.
    """
    caller = _caller(ctx)
    return await get_manager().recent(caller.person, limit, status, state)


@mcp.tool()
async def answer_assignment(id: int, text: str, ctx: Context) -> dict:
    """Give a waiting or paused_limits assignment its answer, and resume it.

    text is the answer, from what you know or from what the person said. For
    a limits stop, "continue" is enough. The manager reads the limits first:
    when a window is still at or over its ceiling, the assignment stays
    parked, the answer is stored, and the reply says when the window resets.
    A parked assignment waits with no time limit.
    """
    person = _person(ctx)
    return await get_manager().answer(await _own(Caller(person=person), id), text)


@mcp.tool()
async def cancel_assignment(id: int, ctx: Context) -> dict:
    """Stop an assignment. The runner writes its report; the plan and the report stay."""
    person = _person(ctx)
    return await get_manager().cancel(await _own(Caller(person=person), id))


@mcp.tool()
async def extend_assignment(
    id: int,
    ctx: Context,
    tokens: int | None = None,
    working_time: str | None = None,
    five_hour_pct: int | None = None,
    seven_day_pct: int | None = None,
    max_turns: int | None = None,
) -> dict:
    """Raise one or more caps of an assignment.

    Each value must be more than the current one. A ceiling above the
    maximum in assignments.limits is clamped, and the reply says so. A
    budget_exceeded or time_exceeded assignment continues its session; an
    assignment that ended in another way is refused.
    """
    person = _person(ctx)
    row = await _own(Caller(person=person), id)
    return await get_manager().extend(
        row,
        tokens=tokens,
        working_time=working_time,
        five_hour_pct=five_hour_pct,
        seven_day_pct=seven_day_pct,
        max_turns=max_turns,
    )


# --- HTTP ------------------------------------------------------------------


class BearerAuthMiddleware:
    """Require a known bearer on every path except ``OPEN_PATHS``."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        raw = headers.get(b"authorization", b"").decode("latin-1")
        if find_caller(self.settings, raw) is None:
            logger.warning({"message": "rejected request: missing or wrong token"})
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def healthz(request: Request) -> Response:
    return JSONResponse({"ok": True})


async def readyz(request: Request) -> Response:
    """Booleans and counts only: no person, no goal, and no secret."""
    manager = get_manager()
    database = await manager.store.ping()
    counts = await manager.store.counts() if database else {}
    body: dict[str, Any] = {
        "ok": database,
        "database": database,
        "channels": manager.settings.will_notify,
        "people": len(manager.settings.people),
    }
    body.update({name: counts.get(name, 0) for name in READY_COUNTS})
    return JSONResponse(body, status_code=200 if database else 503)


mcp.custom_route("/healthz", methods=["GET"])(healthz)
mcp.custom_route("/readyz", methods=["GET"])(readyz)


def build_app(
    settings: Settings, store: Store, *, stub_delay_s: float | None = 1.0, **manager_args: Any
) -> ASGIApp:
    """Build the ASGI app: the MCP routes plus the bearer check.

    The app lifespan opens the store and starts the queued assignments.
    ``host="0.0.0.0"`` turns off the loopback-only DNS-rebinding guard of the
    SDK: a caller reaches this container by its compose or cluster name,
    never by ``localhost``.
    """
    global _manager
    manager = Manager(settings, store, stub_delay_s=stub_delay_s, **manager_args)
    _manager = manager
    inner = mcp.streamable_http_app(streamable_http_path="/mcp", host="0.0.0.0")
    session_lifespan = inner.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await manager.open()
        try:
            async with session_lifespan(app):
                logger.info(
                    {
                        "message": "assignments ready",
                        "people": settings.people,
                        "channels": settings.will_notify,
                    }
                )
                yield
        finally:
            await manager.close()

    inner.router.lifespan_context = lifespan
    return BearerAuthMiddleware(inner, settings)
