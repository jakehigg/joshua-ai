"""In-process ``scheduling`` tool server: schedule and manage the caller's tasks.

Every tool acts on the conversation that invoked it (``deps.conversation``), so a
task is always owned by the conversation that created it. ``schedule_task`` takes
either a one-time time (``when``/``in_seconds``) or a recurring ``cron``
expression; the other tools list, cancel, pause, resume, or update a task by id.
Times are read in the configured timezone (``deps.tz``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from joshua_shared.log import get_logger

from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.cron import compute_next_cron, now_in, parse_when
from joshua_core.engine.tools import ToolDeps

logger = get_logger("tools.scheduling")

# A cancelled task leaves the active set the same way a completed one does; the
# status vocabulary has no separate "cancelled" state.
CANCELLED_STATUS = "completed"


def _reply(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _fmt(when: datetime) -> str:
    return when.strftime("%A, %B %d, %Y at %I:%M %p %Z").strip()


def _resolve_time(
    deps: ToolDeps, *, when: str | None, cron: str | None, in_seconds: int | None
) -> tuple[datetime, str | None]:
    """Return ``(next_run_at, cron_expr)`` from the caller's time arguments.

    Raises ``ValueError`` when no time is given or a value cannot be read.
    """
    now = now_in(deps.tz)
    if cron:
        return compute_next_cron(cron, now, deps.tz), cron
    if in_seconds is not None:
        return now + timedelta(seconds=max(0, int(in_seconds))), None
    if when:
        return parse_when(when, deps.tz, now), None
    raise ValueError("give a time: 'when', 'in_seconds', or 'cron'")


async def do_schedule_task(
    deps: ToolDeps,
    *,
    prompt: str,
    when: str | None = None,
    cron: str | None = None,
    in_seconds: int | None = None,
) -> dict[str, Any]:
    """Create a task owned by the calling conversation."""
    prompt = (prompt or "").strip()
    if not prompt:
        return _reply("I need the instruction to run when the task fires.")
    try:
        next_run_at, cron_expr = _resolve_time(deps, when=when, cron=cron, in_seconds=in_seconds)
    except ValueError as exc:
        return _reply(f"I could not schedule that: {exc}")

    task = await deps.repo.create_task(deps.conversation.id, prompt, next_run_at, cron_expr)
    logger.info(
        {
            "message": "task scheduled",
            "task_id": task.id,
            "conversation_id": deps.conversation.id,
            "recurring": bool(cron_expr),
        }
    )
    kind = f"Recurring (`{cron_expr}`)" if cron_expr else "One-time"
    return _reply(f"{kind} task {task.id} set. Next run: {_fmt(next_run_at)}.")


async def do_list_tasks(deps: ToolDeps) -> dict[str, Any]:
    """List the calling conversation's active and paused tasks."""
    tasks = await deps.repo.list_tasks(deps.conversation.id)
    if not tasks:
        return _reply("You have no scheduled tasks.")
    lines = []
    for task in tasks:
        cadence = f"cron `{task.cron_expr}`" if task.cron_expr else "one-time"
        lines.append(
            f"- {task.id} ({task.status}, {cadence}) next {_fmt(task.next_run_at)}: {task.prompt}"
        )
    return _reply("Your scheduled tasks:\n" + "\n".join(lines))


async def do_cancel_task(deps: ToolDeps, *, task_id: str) -> dict[str, Any]:
    ok = await deps.repo.set_task_status(task_id, CANCELLED_STATUS, deps.conversation.id)
    return _reply(f"Cancelled task {task_id}." if ok else _not_yours(task_id))


async def do_pause_task(deps: ToolDeps, *, task_id: str) -> dict[str, Any]:
    ok = await deps.repo.set_task_status(task_id, "paused", deps.conversation.id)
    return _reply(f"Paused task {task_id}." if ok else _not_yours(task_id))


async def do_resume_task(deps: ToolDeps, *, task_id: str) -> dict[str, Any]:
    ok = await deps.repo.set_task_status(task_id, "active", deps.conversation.id)
    return _reply(f"Resumed task {task_id}." if ok else _not_yours(task_id))


async def do_update_task(
    deps: ToolDeps,
    *,
    task_id: str,
    prompt: str | None = None,
    when: str | None = None,
    cron: str | None = None,
) -> dict[str, Any]:
    next_run_at: datetime | None = None
    cron_expr: str | None = None
    try:
        if cron:
            next_run_at, cron_expr = _resolve_time(deps, when=None, cron=cron, in_seconds=None)
        elif when:
            next_run_at, _ = _resolve_time(deps, when=when, cron=None, in_seconds=None)
            cron_expr = ""  # switching to a one-time time clears the cron schedule
    except ValueError as exc:
        return _reply(f"I could not update that: {exc}")
    new_prompt = prompt.strip() if prompt else None
    if new_prompt is None and next_run_at is None:
        return _reply("Tell me what to change: a new prompt, time, or cron.")
    ok = await deps.repo.update_task(
        task_id,
        prompt=new_prompt,
        cron_expr=cron_expr,
        next_run_at=next_run_at,
        conversation_id=deps.conversation.id,
    )
    return _reply(f"Updated task {task_id}." if ok else _not_yours(task_id))


def _not_yours(task_id: str) -> str:
    return f"I found no task {task_id} for this conversation."


# --- server ----------------------------------------------------------------

_SCHEDULE_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "The instruction to carry out when the task fires.",
        },
        "when": {
            "type": "string",
            "description": (
                "A one-time time: 'in 30 minutes', '2026-08-27 09:00', '9:00am', "
                "or 'tomorrow 07:30'. Read in the configured timezone."
            ),
        },
        "in_seconds": {
            "type": "integer",
            "description": "A one-time delay in seconds from now.",
        },
        "cron": {
            "type": "string",
            "description": (
                "A five-field cron expression (minute hour day-of-month month "
                "day-of-week) for a recurring task, e.g. '0 18 * * 4'."
            ),
        },
    },
    "required": ["prompt"],
}

_TASK_ID_SCHEMA = {
    "type": "object",
    "properties": {"task_id": {"type": "string", "description": "The task id."}},
    "required": ["task_id"],
}

_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "description": "The task id."},
        "prompt": {"type": "string", "description": "A new instruction."},
        "when": {"type": "string", "description": "A new one-time time."},
        "cron": {"type": "string", "description": "A new cron expression."},
    },
    "required": ["task_id"],
}


def build_scheduling_server(deps: ToolDeps) -> Any:
    """Build the in-process ``scheduling`` MCP server bound to one conversation."""

    @tool(
        "schedule_task",
        "Schedule an instruction to run later, once or on a cron schedule. Give a "
        "'prompt' and exactly one of 'when', 'in_seconds', or 'cron'.",
        _SCHEDULE_SCHEMA,
    )
    async def schedule_task(args: dict[str, Any]) -> dict[str, Any]:
        return await do_schedule_task(
            deps,
            prompt=args.get("prompt", ""),
            when=args.get("when") or None,
            cron=args.get("cron") or None,
            in_seconds=args.get("in_seconds"),
        )

    @tool("list_tasks", "List this conversation's scheduled tasks.", {})
    async def list_tasks(_args: dict[str, Any]) -> dict[str, Any]:
        return await do_list_tasks(deps)

    @tool("cancel_task", "Cancel a scheduled task by id.", _TASK_ID_SCHEMA)
    async def cancel_task(args: dict[str, Any]) -> dict[str, Any]:
        return await do_cancel_task(deps, task_id=args.get("task_id", ""))

    @tool(
        "pause_task", "Pause a scheduled task so it does not fire until resumed.", _TASK_ID_SCHEMA
    )
    async def pause_task(args: dict[str, Any]) -> dict[str, Any]:
        return await do_pause_task(deps, task_id=args.get("task_id", ""))

    @tool("resume_task", "Resume a paused task.", _TASK_ID_SCHEMA)
    async def resume_task(args: dict[str, Any]) -> dict[str, Any]:
        return await do_resume_task(deps, task_id=args.get("task_id", ""))

    @tool("update_task", "Change a task's prompt, time, or cron schedule.", _UPDATE_SCHEMA)
    async def update_task(args: dict[str, Any]) -> dict[str, Any]:
        return await do_update_task(
            deps,
            task_id=args.get("task_id", ""),
            prompt=args.get("prompt") or None,
            when=args.get("when") or None,
            cron=args.get("cron") or None,
        )

    return create_sdk_mcp_server(
        name="scheduling",
        version="1.0.0",
        tools=[schedule_task, list_tasks, cancel_task, pause_task, resume_task, update_task],
    )


def register(manager: Any) -> None:
    """Wire the ``scheduling`` builtin into the conversation manager."""
    manager.register_builtin("scheduling", build_scheduling_server)
