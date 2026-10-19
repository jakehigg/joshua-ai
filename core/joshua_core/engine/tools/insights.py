"""In-process ``insights`` tool server: read the insights, resolve them, and start a run.

``list_insights`` lists the insights in ``wiki/insights/``,
``acknowledge_insight`` and ``dismiss_insight`` change the status of one, and
``run_insights`` starts a run of the ``insights`` job through the job runner,
so a run from chat is the same as a run from the laptop. A tool names an
insight by ``<section>/<slug>``, its path under ``wiki/insights/`` without
``.md``.

Every tool gates on the current speaker being a member: a guest, or a speaker
core does not know, gets a "not available" reply and nothing changes.

A status change takes the lock that the jobs and the nightly reflection
share, writes the frontmatter of the page and ``insights/README.md``, and
commits the wiki with the message ``insights: <id> <status> by <person>``.

Core registers this server only when ``jobs.enabled`` and
``jobs.insights.enabled`` are both true.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from joshua_shared import layout, wikigit
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.tools import ToolDeps
from joshua_core.jobs.insights.job import local_day
from joshua_core.jobs.insights.page import (
    OPEN_STATUSES,
    STATUSES,
    index_order,
    load_insight,
    load_insights,
    set_fields,
    write_index,
)
from joshua_core.jobs.research.text import inline
from joshua_core.jobs.runner import Clock, JobArgsError, JobBusy, JobRunner, UnknownJob, utc_now

logger = get_logger("tools.insights")

JOB = "insights"
NOT_AVAILABLE = "That is not available to you. Only a member can use the insights."
BUSY = "A job or the nightly reflection is writing the wiki now. Try again in a few minutes."
MAX_LIST = 50
LIST_STATUSES = ("open", "all", *STATUSES)


@dataclass(frozen=True)
class InsightsContext:
    """What the tools need beyond the conversation: the config, the data root,
    the wiki lock, the job runner, and the clock."""

    settings: JoshuaConfig
    data_dir: Path
    lock: asyncio.Lock
    jobs: JobRunner
    clock: Clock = utc_now


def _reply(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


async def _member_id(deps: ToolDeps) -> str | None:
    """The id of the current speaker if they are a member, else None."""
    pid = deps.person_id
    if not pid:
        return None
    person = await deps.repo.get_person(pid)
    if person is None or person.role != "member":
        return None
    return person.id


def _limit(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = 20
    return max(1, min(number, MAX_LIST))


async def do_list_insights(
    deps: ToolDeps,
    ctx: InsightsContext,
    *,
    status: str | None = "open",
    topic: str | None = None,
    limit: Any = 20,
) -> dict[str, Any]:
    if await _member_id(deps) is None:
        return _reply(NOT_AVAILABLE)
    wanted = (status or "open").strip().lower()
    if wanted not in LIST_STATUSES:
        return _reply(f"The status must be one of {', '.join(LIST_STATUSES)}.")
    items = load_insights(ctx.data_dir, (topic or "").strip() or None)
    if wanted == "open":
        items = [i for i in items if i.insight.status in OPEN_STATUSES]
    elif wanted != "all":
        items = [i for i in items if i.insight.status == wanted]
    items = index_order(items)[: _limit(limit)]
    if not items:
        return _reply(f"There are no {wanted} insights.")
    lines = []
    for item in items:
        i = item.insight
        lines.append(
            f"- `{item.id}` [{i.status}, {i.severity}] {inline(i.claim, 300)} "
            f"(topic {i.topic}, valid until {i.valid_until.isoformat()}, found "
            f"{i.found_at.isoformat()})"
        )
    return _reply("Insights:\n" + "\n".join(lines))


async def _set_status(
    deps: ToolDeps,
    ctx: InsightsContext,
    *,
    insight_id: str,
    status: str,
    allowed_from: tuple[str, ...],
    note: str | None = None,
) -> dict[str, Any]:
    member = await _member_id(deps)
    if member is None:
        return _reply(NOT_AVAILABLE)
    insight_id = (insight_id or "").strip()
    if insight_id.endswith(".md"):
        insight_id = insight_id[:-3]
    if load_insight(insight_id, ctx.data_dir) is None:
        return _reply(f"I found no insight `{insight_id}`. Give it as <section>/<slug>.")
    if ctx.lock.locked():
        return _reply(BUSY)
    async with ctx.lock:
        current = load_insight(insight_id, ctx.data_dir)
        if current is None:
            return _reply(f"I found no insight `{insight_id}`.")
        if current.insight.status not in allowed_from:
            return _reply(
                f"Insight `{insight_id}` is {current.insight.status}, so I did not change it."
            )
        keys: dict[str, Any] = {
            "status": status,
            "resolved_at": local_day(ctx.clock(), ctx.settings),
            "resolved_by": member,
        }
        if note is not None:
            keys["resolution_note"] = note
        changed = set_fields(current, ctx.data_dir, **keys)
        written = [changed.path, write_index(ctx.data_dir)]
        if wikigit.is_enabled(ctx.settings):
            wikigit.commit(
                layout.wiki_root(ctx.data_dir),
                written,
                f"insights: {insight_id} {status} by {member}",
            )
    logger.info({"message": "insight status changed", "status": status, "person": member})
    return _reply(f"Insight `{insight_id}` is {status} now.")


async def do_acknowledge_insight(
    deps: ToolDeps, ctx: InsightsContext, *, insight_id: str
) -> dict[str, Any]:
    return await _set_status(
        deps, ctx, insight_id=insight_id, status="acknowledged", allowed_from=("new", "told")
    )


async def do_dismiss_insight(
    deps: ToolDeps, ctx: InsightsContext, *, insight_id: str, why: str
) -> dict[str, Any]:
    if await _member_id(deps) is None:
        return _reply(NOT_AVAILABLE)
    reason = " ".join((why or "").split())
    if not reason:
        return _reply("Tell me why, so a later run knows why the insight is closed.")
    return await _set_status(
        deps,
        ctx,
        insight_id=insight_id,
        status="dismissed",
        allowed_from=OPEN_STATUSES,
        note=reason[:1000],
    )


async def do_run_insights(deps: ToolDeps, ctx: InsightsContext, *, topic: str) -> dict[str, Any]:
    if await _member_id(deps) is None:
        return _reply(NOT_AVAILABLE)
    try:
        run_id = await ctx.jobs.start(JOB, {"topic": topic}, dry_run=False)
    except JobBusy:
        return _reply(BUSY)
    except JobArgsError as exc:
        return _reply(f"I did not start the insights run: {exc}.")
    except UnknownJob:
        return _reply("The insights job is not turned on for this instance.")
    return _reply(
        f"Started insights run {run_id}. It works in the background. When it ends, "
        "its report is a journal entry, and list_insights shows what it found."
    )


# --- server ----------------------------------------------------------------

_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "description": "open (the default: new, told, and acknowledged), new, told, "
            "acknowledged, dismissed, expired, or all.",
        },
        "topic": {"type": "string", "description": "Only the insights of this topic page."},
        "limit": {"type": "integer", "description": "How many insights, 1 to 50. Default 20."},
    },
}

_ID_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "The insight, as <section>/<slug>."},
    },
    "required": ["slug"],
}

_DISMISS_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "The insight, as <section>/<slug>."},
        "why": {"type": "string", "description": "Why the insight does not apply."},
    },
    "required": ["slug", "why"],
}

_RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "description": "A topic page, such as garden/irrigation.md."},
    },
    "required": ["topic"],
}


def build_insights_server(deps: ToolDeps, ctx: InsightsContext) -> Any:
    """Build the in-process ``insights`` MCP server bound to one conversation."""

    @tool(
        "list_insights",
        "List the insights: places where what we do is different from what is known, "
        "for the date and the season. Only a member may call this.",
        _LIST_SCHEMA,
    )
    async def list_insights(args: dict[str, Any]) -> dict[str, Any]:
        return await do_list_insights(
            deps,
            ctx,
            status=args.get("status") or "open",
            topic=args.get("topic"),
            limit=args.get("limit") or 20,
        )

    @tool(
        "acknowledge_insight",
        "Mark one insight as acknowledged: the person knows about it. Only a member may call this.",
        _ID_SCHEMA,
    )
    async def acknowledge_insight(args: dict[str, Any]) -> dict[str, Any]:
        return await do_acknowledge_insight(deps, ctx, insight_id=args.get("slug") or "")

    @tool(
        "dismiss_insight",
        "Close one insight that does not apply, with the reason. A later run does not "
        "raise it again. Only a member may call this.",
        _DISMISS_SCHEMA,
    )
    async def dismiss_insight(args: dict[str, Any]) -> dict[str, Any]:
        return await do_dismiss_insight(
            deps, ctx, insight_id=args.get("slug") or "", why=args.get("why") or ""
        )

    @tool(
        "run_insights",
        "Start an insights run on one topic that has knowledge pages. Only a member may call this.",
        _RUN_SCHEMA,
    )
    async def run_insights(args: dict[str, Any]) -> dict[str, Any]:
        return await do_run_insights(deps, ctx, topic=args.get("topic") or "")

    return create_sdk_mcp_server(
        name="insights",
        version="1.0.0",
        tools=[list_insights, acknowledge_insight, dismiss_insight, run_insights],
    )


def register(
    manager: Any,
    *,
    settings: JoshuaConfig,
    data_dir: Path | str,
    lock: asyncio.Lock,
    jobs: JobRunner,
    clock: Clock | None = None,
) -> bool:
    """Wire the ``insights`` builtin into the conversation manager when
    ``jobs.enabled`` and ``jobs.insights.enabled`` are both true. Returns True
    when it did."""
    if not (settings.jobs.enabled and settings.jobs.insights.enabled):
        return False
    ctx = InsightsContext(
        settings=settings,
        data_dir=Path(data_dir),
        lock=lock,
        jobs=jobs,
        clock=clock or utc_now,
    )
    manager.register_builtin("insights", lambda deps: build_insights_server(deps, ctx))
    return True
