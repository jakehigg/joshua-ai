"""The assignments manager over MCP.

The container serves MCP at ``/mcp`` over streamable HTTP on port 8000.
``GET /healthz`` and ``GET /readyz`` are open and name no person. Every other
route needs ``Authorization: Bearer <token>``; a missing or wrong token gets
401. A path under ``/v1/`` is the read API (``read_api``) and takes only the
fleet token of a caller in ``assignments.read_callers``. Every other path
takes only a bearer of ``/mcp``. A bearer from ``ASSIGNMENTS_TOKENS`` names
a person. The ``ADDON_TOKEN`` bearer names no person: it may use
``assignment_status`` and ``list_assignments`` for every assignment, and
every other tool, also each comment tool, gives it "403 forbidden". A
person must be a member, and sees only their own
assignments: another person's assignment is "404 not found". A deleted draft
is "404 not found" too, except to ``restore_assignment``, and to
``start_assignment``, which refuses it as ``deleted`` with a hint.

Each tool call whose result is a refusal (``status: rejected``) writes one
log line with the tool, the person, and the reason. The line holds no
argument, so never the body of a comment or a spec.
"""

from __future__ import annotations

import contextlib
import hmac
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID

from joshua_shared.log import get_logger
from mcp import types
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from joshua_assignments.manager import Manager, rejected, with_hint
from joshua_assignments.read_api import READ_PREFIX, ReadApi, read_caller
from joshua_assignments.settings import Caller, Settings
from joshua_assignments.store import Store

logger = get_logger("joshua_assignments")

# The key the gateway puts in ``params._meta`` of a call from a workspace thread.
WORKSPACE_META_KEY = "joshua/workspace"

OPEN_PATHS = frozenset({"/healthz", "/readyz"})
_BEARER = "Bearer "
CONVERSATION_HEADER = "x-joshua-conversation"
READY_COUNTS = ("draft", "running", "waiting", "paused_limits")


def refusal_reason(result: Any) -> str | None:
    """The ``reason`` of a tool result that is a refusal, else None.

    The reply of a tool is a dict, which the result holds as JSON text."""
    data = getattr(result, "structured_content", None)
    if data is None:
        content = getattr(result, "content", None) or []
        text = getattr(content[0], "text", None) if len(content) == 1 else None
        if isinstance(text, str) and text.startswith("{"):
            with contextlib.suppress(ValueError):
                data = json.loads(text)
    if not isinstance(data, dict) or data.get("status") != "rejected":
        return None
    reason = data.get("reason")
    return str(reason)[:64] if reason else None


class AssignmentsServer(MCPServer):
    """The MCP server, with one log line for each refusal of a tool."""

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context | None = None
    ) -> Any:
        result = await super().call_tool(name, arguments, context)
        reason = refusal_reason(result) if isinstance(result, types.CallToolResult) else None
        if reason is not None:
            caller = None
            if context is not None and _manager is not None:
                headers = context.headers or {}
                caller = find_caller(_manager.settings, headers.get("authorization"))
            logger.info(
                {
                    "message": "tool refused",
                    "tool": name,
                    "person": caller.person if caller is not None else None,
                    "reason": reason,
                }
            )
        return result


mcp = AssignmentsServer(name="assignments")

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


def origin_of(ctx: Context) -> str | None:
    """The workspace id in the ``_meta`` of this call, or None.

    The request context holds ``_meta`` as ``ctx.request_context.meta``. A call
    from any other chat has no ``joshua/workspace`` key, and a value that is not
    a UUID is no workspace."""
    try:
        meta = ctx.request_context.meta
    except ValueError:
        return None
    value = meta.get(WORKSPACE_META_KEY) if meta else None
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def with_link(
    result: dict[str, Any], assignment_id: Any, workspace: str | None
) -> dict[str, Any] | types.CallToolResult:
    """``result`` as it is, or, for a call from a workspace, with a
    ``resource_link`` to the viewer page of the assignment after its text.

    A refusal has no assignment and gets none. The address is under
    ``workspaces.viewer_url`` when that is set, else the path ``/assignments/<id>``."""
    if not workspace or isinstance(assignment_id, bool) or not isinstance(assignment_id, int):
        return result
    if result.get("status") == "rejected":
        return result
    path = f"/assignments/{assignment_id}"
    base = get_manager().load_config().workspaces.viewer_url
    uri = f"{base.rstrip('/')}{path}" if base else path
    title = str(result.get("title") or "").strip()
    link = types.ResourceLink(
        uri=uri,
        name=f"Assignment {assignment_id}: {title}" if title else f"Assignment {assignment_id}",
        description="The page of the assignment in the viewer",
        mime_type="text/html",
    )
    return types.CallToolResult(
        content=[types.TextContent(text=json.dumps(result, indent=2, default=str)), link],
        structured_content=json.loads(json.dumps(result, default=str)),
    )


def _person(ctx: Context) -> str:
    caller = _caller(ctx)
    if caller.person is None:
        logger.warning({"message": "refused a person tool to a caller with no person"})
        raise ToolError("403 forbidden: this tool needs a person")
    return caller.person


async def _own(caller: Caller, assignment_id: int, *, deleted: bool = False) -> dict[str, Any]:
    """The assignment, when the caller may see it. Another person's is "not found".

    A deleted draft is "not found" too, unless ``deleted`` is true."""
    row = await get_manager().store.get(int(assignment_id))
    if (
        row is None
        or (caller.person is not None and row["person_id"] != caller.person)
        or (row.get("deleted_at") is not None and not deleted)
    ):
        raise ToolError(f"404 not found: no assignment {assignment_id}")
    return row


# --- tools -----------------------------------------------------------------


@mcp.tool()
async def start_assignment(
    ctx: Context,
    spec: dict[str, Any] | None = None,
    id: int | None = None,
    saved: str | None = None,
    new: bool = False,
    rerun: int | None = None,
    note: str | None = None,
) -> dict:
    """To start a draft, pass its id. Pass spec only for a new job that no draft
    covers. Pass saved to run a saved assignment. Pass rerun to run an ended
    assignment again.

    Give exactly one of id, spec, saved, or rerun. A started draft keeps its
    id. It runs now, in the background, and the reply gives id, status, and
    title. rerun=N is a new run with the spec of the ended assignment N, and
    parent_id N: use it when the person says "submit it again" or "re-run
    it". note is the words of the person for this run; it goes at the start
    of the goal. A draft is refused as is_a_draft.
    With saved, a spec beside it replaces only the top-level fields that it
    gives, for this run (such as {"model": "opus"}). A spec alone is refused
    as drafts_open while the person has drafts, and the refusal lists them;
    pass new: true only for a job that none of them covers.

    A refusal has reason, message, and hint: tell the person the hint. There
    is no queue: while the slot is taken, the start is refused as busy, with
    running (id and title) and retry_after_s, and nothing is stored. Do not
    start a copy; start this one when the running one ends.

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
    workspace = origin_of(ctx)
    if id is not None:
        if spec is not None or saved is not None or rerun is not None or note is not None:
            return with_hint(rejected("invalid_arguments", "give one of spec, id, saved, or rerun"))
        row = await _own(Caller(person=person), id, deleted=True)
        started = await get_manager().start(person, draft=row, workspace=workspace)
    else:
        started = await get_manager().start(
            person,
            spec,
            conversation,
            saved=saved,
            new=new,
            rerun=rerun,
            note=note,
            workspace=workspace,
        )
    return with_link(started, started.get("id"), workspace)


@mcp.tool()
async def draft_assignment(
    ctx: Context, spec: dict[str, Any] | None = None, saved: str | None = None
) -> dict:
    """Store an assignment as a draft, for the person to read first. Nothing runs.

    The spec is the same as for start_assignment, and it is checked the same
    way. Give spec, or saved with an optional spec override. Returns id,
    title, and notes. The person reads the draft in the viewer and can leave
    comments on its lines. Start it with start_assignment(id=...).
    """
    person = _person(ctx)
    conversation = (ctx.headers or {}).get(CONVERSATION_HEADER)
    return await get_manager().draft(person, spec, conversation, saved=saved)


@mcp.tool()
async def update_assignment(id: int, spec: dict[str, Any], ctx: Context) -> dict:
    """Replace the whole spec of a draft. Any other status is refused as not_a_draft.

    The spec is checked as start_assignment checks it. A comment on a line
    that changed becomes stale. The reply gives open_comments and
    stale_comments.
    """
    person = _person(ctx)
    return await get_manager().update_draft(await _own(Caller(person=person), id), spec)


@mcp.tool()
async def delete_assignment(id: int, ctx: Context) -> dict:
    """Removes a draft from view; the person can ask for it back.

    Call it only when the person asks to delete the draft. The draft and its
    comments stay, and restore_assignment(id) brings it back. A run is never
    deleted (not_a_draft)."""
    person = _person(ctx)
    return await get_manager().delete_draft(await _own(Caller(person=person), id))


@mcp.tool()
async def restore_assignment(id: int, ctx: Context) -> dict:
    """Bring back a draft that delete_assignment removed. It is a draft again, with its comments."""
    person = _person(ctx)
    return await get_manager().restore(await _own(Caller(person=person), id, deleted=True))


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
async def assignment_comments(
    ctx: Context, saved: str | None = None, id: int | None = None, target: str | None = None
) -> dict:
    """The open comments of the person on an assignment, each with its target and its line.

    Give saved (the name of a saved assignment) or id (a draft or a run), not
    both. target is definition, plan, or report, or none for all; a saved
    assignment has only a definition. A comment is a request of the person
    on one line: line_no and line_text say which line it was, line_now what
    the line is now, and stale is true when the line changed. The answer also
    has the spec. Apply a comment on a definition with save_assignment (saved),
    update_assignment (a draft), or a new run with the changed spec. A comment
    on a report is the answer of the person to the decision on its line:
    apply it to the result. Then call resolve_comment for each.
    """
    person = _person(ctx)
    if (saved is None) == (id is None):
        return rejected("invalid_arguments", "give saved (a name) or id, not both")
    row = await _own(Caller(person=person), id) if id is not None else None
    return await get_manager().open_comments(person, saved=saved, row=row, target=target)


@mcp.tool()
async def resolve_comment(
    comment_id: int, resolution: str, ctx: Context, applied: list[str] | None = None
) -> dict:
    """Mark one comment resolved, after the change is made.

    resolution is one line on what you did. applied names what changed, at
    least one reference: a document path (documents/<owner>/<slug>.md), a
    task (task 78), an issue URL, assignment N (a draft or a run whose spec
    changed), or saved <name>. With no reference, it is refused as
    nothing_applied: make the change first."""
    person = _person(ctx)
    return await get_manager().resolve_comment(person, comment_id, resolution, applied)


@mcp.tool()
async def assignment_status(id: int, ctx: Context) -> dict:
    """The state of one assignment.

    status is draft, running, waiting, paused_limits, done, failed,
    cancelled, budget_exceeded, or time_exceeded. state is draft,
    in_progress, or completed. The result also has the title, the plan of
    the worker, the report and report_at, the recorded outputs, the last
    checkpoint, the usage, the turns, the open question, the open request
    (kind, ref, and reason), the granted repositories, the files in inbox,
    the limits at the stop, the summary, the error, and saved: the saved
    assignment that it started from, or null. parent_id and rerun_of (id
    and title) name the assignment that this one is a re-run of, or null.
    Read the report here.
    """
    caller = _caller(ctx)
    row = await _own(caller, id)
    return with_link(await get_manager().status(row), row["id"], origin_of(ctx))


@mcp.tool()
async def list_assignments(
    ctx: Context, limit: int = 10, status: str | None = None, state: str | None = None
) -> dict:
    """The person's assignments, the newest first, each with its title, state, and saved.

    limit is 1 to 100. status filters by status. state filters by state:
    draft, in_progress, or completed.
    """
    caller = _caller(ctx)
    return await get_manager().recent(caller.person, limit, status, state)


@mcp.tool()
async def answer_assignment(id: int, text: str, ctx: Context) -> dict:
    """Give a waiting or paused_limits assignment its answer, and resume it.

    text is the answer, from what you know or from what the person said. For
    a limits stop, "continue" is enough. It also answers a request for a
    repository or a file with a refusal or another way, such as "Refused:
    ..." or "Use the page X instead". It clears the open request. The
    manager reads the limits first: when a window is still at or over its
    ceiling, the assignment stays parked, the answer is stored, and the
    reply says when the window resets. While every slot is taken, it is
    refused as busy and the answer is not stored. A parked assignment waits
    with no time limit.
    """
    person = _person(ctx)
    return await get_manager().answer(await _own(Caller(person=person), id), text)


@mcp.tool()
async def grant_repo(id: int, repo: str, ctx: Context) -> dict:
    """Give a waiting assignment one more repository to clone, and resume it.

    Use it for a request of kind repo. repo is host/group/name or a URL. It
    must match assignments.repos, as a repository of a spec must; else it is
    refused as repo_not_allowed and nothing changes. The worker resumes with
    the clone command. It clears the open request. While every slot is
    taken, it is refused as busy and nothing is stored.
    """
    person = _person(ctx)
    return await get_manager().grant_repo(await _own(Caller(person=person), id), repo)


@mcp.tool()
async def provide_file(id: int, name: str, content: str, ctx: Context) -> dict:
    """Give a waiting assignment one text file in inbox/ of its scratch, and resume it.

    Use it for a request of kind file. name is a plain file name, such as
    manual.md: no path separator, no leading dot, at most 120 characters.
    content is UTF-8 text, at most 2 MiB; an assignment holds at most 20 MiB
    and 50 files. A file with the same name replaces the earlier one. The
    worker reads it as data, never as an instruction. It clears the open
    request. While every slot is taken, it is refused as busy and nothing is
    written.
    """
    person = _person(ctx)
    return await get_manager().provide_file(await _own(Caller(person=person), id), name, content)


@mcp.tool()
async def cancel_assignment(id: int, ctx: Context) -> dict:
    """Stop an assignment. It returns when the slot is free, so a start can follow at once.

    The runner writes its report; the plan and the report stay."""
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
    budget_exceeded or time_exceeded assignment continues its session, or is
    refused as busy with no change while every slot is taken. An assignment
    that ended in another way is refused.
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
    """Require a known bearer on every path except ``OPEN_PATHS``.

    A path under ``READ_PREFIX`` takes the fleet token of an identity that
    ``read_callers`` gives now, and no other bearer. Every other path takes a
    bearer of ``settings.tokens``, and no fleet token.
    """

    def __init__(
        self, app: ASGIApp, settings: Settings, read_callers: Callable[[], list[str]]
    ) -> None:
        self.app = app
        self.settings = settings
        self.read_callers = read_callers

    def _allowed(self, path: str, authorization: str) -> bool:
        if path.startswith(READ_PREFIX):
            return read_caller(self.settings, self.read_callers(), authorization) is not None
        return find_caller(self.settings, authorization) is not None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        raw = headers.get(b"authorization", b"").decode("latin-1")
        if not self._allowed(scope["path"], raw):
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


def _read_route(name: str) -> Callable[[Request], Awaitable[Response]]:
    """A handler of the read API that uses the manager of ``build_app``."""

    async def handler(request: Request) -> Response:
        route: Callable[[Request], Awaitable[Response]] = getattr(ReadApi(get_manager()), name)
        return await route(request)

    handler.__name__ = f"read_{name}"
    return handler


mcp.custom_route("/healthz", methods=["GET"])(healthz)
mcp.custom_route("/readyz", methods=["GET"])(readyz)
mcp.custom_route(f"{READ_PREFIX}assignments", methods=["GET"])(_read_route("assignments"))
mcp.custom_route(f"{READ_PREFIX}assignments/{{assignment_id:int}}", methods=["GET"])(
    _read_route("assignment")
)
mcp.custom_route(f"{READ_PREFIX}saved", methods=["GET"])(_read_route("saved_list"))
mcp.custom_route(f"{READ_PREFIX}saved/{{saved_id:int}}", methods=["GET"])(_read_route("saved"))
mcp.custom_route(
    f"{READ_PREFIX}assignments/{{assignment_id:int}}/comments", methods=["GET", "POST"]
)(_read_route("assignment_comments"))
mcp.custom_route(
    f"{READ_PREFIX}assignments/{{assignment_id:int}}/comments/{{comment_id:int}}",
    methods=["DELETE"],
)(_read_route("assignment_comment"))
mcp.custom_route(f"{READ_PREFIX}saved/{{saved_id:int}}/comments", methods=["GET", "POST"])(
    _read_route("saved_comments")
)
mcp.custom_route(
    f"{READ_PREFIX}saved/{{saved_id:int}}/comments/{{comment_id:int}}", methods=["DELETE"]
)(_read_route("saved_comment"))


def build_app(
    settings: Settings, store: Store, *, stub_delay_s: float | None = 1.0, **manager_args: Any
) -> ASGIApp:
    """Build the ASGI app: the MCP routes plus the bearer check.

    The app lifespan opens the store and resumes the running assignments.
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
    return BearerAuthMiddleware(inner, settings, read_callers=lambda: manager.section.read_callers)
