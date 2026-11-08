"""In-process ``limits`` tool server: how much of the Claude subscription is used.

One read-only tool, ``check_limits()``, with no argument. It gives a short
summary in plain words, then the numbers of ``GET /v1/limits`` as JSON. It
reads the same tracker as the route: the last rate-limit report of a model
call on the subscription. It makes no upstream request.

The server goes to the ``chat`` job when ``limits.chat_tool`` is on and to
the ``voice`` job when ``limits.voice_tool`` is on. ``limits.enabled: false``
gives it to no job.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from joshua_shared.config import JoshuaConfig
from joshua_shared.contracts import LimitsResponse, LimitsWindow

from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.tools import ToolDeps
from joshua_core.limits import LimitsTracker

SERVER_NAME = "limits"
TOOL_NAME = "check_limits"

_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def _reply(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _when(value: str | None) -> str | None:
    """A reset time as ``Sat 7 Nov, 10:30 pm``, in the offset it carries."""
    if not value:
        return None
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return None
    hour = at.hour % 12 or 12
    half = "am" if at.hour < 12 else "pm"
    return f"{at:%a} {at.day} {at:%b}, {hour}:{at:%M} {half}"


def _pct(value: float) -> str:
    return f"{value:g}%"


def _window_line(label: str, window: LimitsWindow | None) -> str | None:
    if window is None:
        return None
    when = _when(window.resets_at)
    if window.reset:
        line = f"The {label} reset"
        line += f" at {when}." if when else "."
        return line + " Its numbers update on the next model call."
    line = f"{_pct(window.left_pct)} of the {label} is left ({_pct(window.used_pct)} used)."
    if when:
        line += f" It resets {when}."
    return line


def summarize(answer: LimitsResponse, tz: str) -> str:
    """A few short sentences about ``answer``, for the agent to say."""
    if not answer.available:
        return f"The subscription limits are not known: {answer.reason or 'no data'}."
    lines = [
        line
        for line in (
            _window_line("5-hour limit", answer.five_hour),
            _window_line("weekly limit", answer.seven_day),
        )
        if line
    ]
    if not lines:
        lines.append("Anthropic gave no limit numbers.")
    lines.append(f"Times are in {tz}.")
    return " ".join(lines)


def do_check_limits(tracker: LimitsTracker, tz: str) -> str:
    """The summary, then the numbers as JSON."""
    answer = tracker.answer()
    numbers = json.dumps(answer.model_dump(), separators=(",", ":"))
    return f"{summarize(answer, tz)}\n\nNumbers: {numbers}"


def build_limits_server(deps: ToolDeps, tracker: LimitsTracker) -> Any:
    """Build the in-process ``limits`` MCP server."""

    @tool(
        TOOL_NAME,
        "How much of the Claude subscription limits is used and left: the 5-hour "
        "window and the weekly window, in percent, and when each resets. Read-only.",
        _SCHEMA,
    )
    async def check_limits(args: dict[str, Any]) -> dict[str, Any]:
        return _reply(do_check_limits(tracker, deps.tz))

    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=[check_limits])


def register(manager: Any, tracker: LimitsTracker | None, settings: JoshuaConfig) -> None:
    """Wire the ``limits`` builtin for the jobs that ``limits`` turns it on for."""
    jobs = settings.limits.tool_jobs()
    if tracker is None or not jobs:
        return
    manager.register_builtin(
        SERVER_NAME, lambda deps: build_limits_server(deps, tracker), jobs=jobs
    )
