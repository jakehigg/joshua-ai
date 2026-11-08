"""The Claude Agent SDK session of an assignment. The one module that imports the SDK.

A session holds the file tools and Bash (``BASE_TOOLS``) in the scratch
directory of the assignment, the gateway MCP servers that the person may
reach, and the in-process ``assignment`` server. It never holds WebSearch or
WebFetch, never holds the ``assignments`` server, and loads no settings from
the disk (``setting_sources=[]``). ``check_session_tools`` asserts this on
every set of options before a session starts.

The SDK starts the Claude CLI with the environment of this process plus
``options.env``. ``session_env`` gives every variable of this process that
is not in ``KEEP_ENV`` an empty value, so the database URL and the bearers of
the manager never reach the session. The CLI sends its model calls to core's
forwarder with the fleet token of the container.

``run_slice`` runs one slice: one query, at most ``max_turns`` model turns.
It stops early, with an interrupt, when a ``RateLimitEvent`` crosses a
ceiling, when an ``assignment`` tool asks for a stop, or when the working
clock has no time left. A failure gives a short label, never the error text,
because the text can name a host.
"""

from __future__ import annotations

import asyncio
import contextlib
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    McpSdkServerConfig,
    create_sdk_mcp_server,
    tool,
)
from joshua_shared.contracts import MODEL_FORWARDER_PATH
from joshua_shared.log import get_logger

from joshua_assignments.clock import Clock

logger = get_logger("joshua_assignments.session")

BASE_TOOLS = ("Read", "Edit", "Write", "Bash", "Glob", "Grep")
FORBIDDEN_TOOLS = ("WebFetch", "WebSearch")
ASSIGNMENT_SERVER = "assignment"
# The MCP server of this container. A session never holds it, so an
# assignment cannot start an assignment.
MANAGER_SERVER = "assignments"
MAX_BUFFER_SIZE = 32 * 1024 * 1024
WATCH_S = 1.0
STOP_TIMEOUT_S = 15.0
# The result of a slice that used all of its turns. It is not a failure.
MAX_TURNS_SUBTYPE = "error_max_turns"

# Switches that turn off what the Claude CLI sends to other hosts than
# ANTHROPIC_BASE_URL.
QUIET_ENV = {
    "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
# git in a session never asks for a password and reads no system config.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
# The variables of this process that a session keeps. Every other variable
# gets an empty value.
KEEP_ENV = frozenset({"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "TMPDIR", "USER"})

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

# The ``assignment`` servers that ``assignment_server`` made, each with its
# handlers by name. ``check_session_tools`` accepts only one of these.
_ASSIGNMENT_SERVERS: weakref.WeakKeyDictionary[Any, dict[str, ToolHandler]] = (
    weakref.WeakKeyDictionary()
)

TOOL_SPECS: dict[str, tuple[str, dict[str, Any]]] = {
    "checkpoint": (
        "Record your progress. End the note with 'Next: <the next step>'.",
        {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
    ),
    "ask": (
        "Stop the assignment and send one clear question to Joshua. The answer "
        "arrives in a later message.",
        {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    ),
    "wait": (
        "Wait up to 600 seconds, for example for a pipeline. Working time runs.",
        {
            "type": "object",
            "properties": {"seconds": {"type": "integer", "minimum": 1, "maximum": 600}},
            "required": ["seconds"],
        },
    ),
    "finish": (
        "End the assignment. Write the summary on the plan page first. "
        "follow_ups is a list of short texts.",
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "follow_ups": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["summary"],
        },
    ),
}


def session_env(
    environ: Mapping[str, str], *, core_url: str, token: str, home: Path, config_dir: Path
) -> dict[str, str]:
    """The ``options.env`` of a session.

    The CLI sends its model calls to core's forwarder, with the fleet token
    of the container as its Claude token. Core puts the real credential on
    them.
    """
    env = {name: "" for name in environ if name not in KEEP_ENV}
    env.update(
        {
            "ANTHROPIC_BASE_URL": core_url.rstrip("/") + MODEL_FORWARDER_PATH,
            "CLAUDE_CODE_OAUTH_TOKEN": token,
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(config_dir),
            **QUIET_ENV,
            **GIT_ENV,
        }
    )
    return env


def assignment_server(handlers: Mapping[str, ToolHandler]) -> McpSdkServerConfig:
    """The in-process MCP server ``assignment``, with the four tools of ``TOOL_SPECS``."""
    if set(handlers) != set(TOOL_SPECS):
        raise ValueError(f"the assignment server has the tools {sorted(TOOL_SPECS)}")
    tools = [
        tool(name, description, schema)(handlers[name])
        for name, (description, schema) in TOOL_SPECS.items()
    ]
    server = create_sdk_mcp_server(name=ASSIGNMENT_SERVER, tools=tools)
    _ASSIGNMENT_SERVERS[server["instance"]] = dict(handlers)
    return server


def server_handlers(server: Any) -> dict[str, ToolHandler] | None:
    """The handlers of an ``assignment`` server that ``assignment_server`` made, or None."""
    instance = server.get("instance") if isinstance(server, Mapping) else None
    if instance is None:
        return None
    try:
        return _ASSIGNMENT_SERVERS.get(instance)
    except TypeError:
        return None


def check_session_tools(options: Any, gateway_names: set[str], gateway_url: str) -> None:
    """Assert the boundary of a session. Raises AssertionError on any break.

    - ``tools`` is a list of names from ``BASE_TOOLS``, and no other built-in tool;
    - WebSearch and WebFetch are refused;
    - ``mcp_servers`` holds only the gateway servers in ``gateway_names``, each
      an HTTP server on the gateway, and the ``assignment`` server that
      ``assignment_server`` made; never ``assignments``;
    - the session loads no settings and no other MCP config.
    """
    tools = options.tools
    if not isinstance(tools, list) or set(tools) - set(BASE_TOOLS):
        raise AssertionError("a session holds only the file tools and Bash")
    allowed = list(options.allowed_tools or [])
    if set(allowed) & set(FORBIDDEN_TOOLS) or set(tools) & set(FORBIDDEN_TOOLS):
        raise AssertionError("a session may not hold WebSearch or WebFetch")
    if not set(FORBIDDEN_TOOLS) <= set(options.disallowed_tools or []):
        raise AssertionError("a session must refuse WebSearch and WebFetch")
    servers = options.mcp_servers
    if not isinstance(servers, dict):
        raise AssertionError("mcp_servers must be a map")
    if MANAGER_SERVER in servers:
        raise AssertionError("a session may not hold the assignments server")
    base = gateway_url.rstrip("/")
    for name, server in servers.items():
        if name == ASSIGNMENT_SERVER:
            if server_handlers(server) is None:
                raise AssertionError("the assignment server must come from assignment_server")
            continue
        if name not in gateway_names:
            raise AssertionError(f"the MCP server {name!r} is not in the person's gateway list")
        if not isinstance(server, Mapping) or server.get("type") != "http":
            raise AssertionError(f"the MCP server {name!r} must be an HTTP server")
        if server.get("url") != f"{base}/{name}":
            raise AssertionError(f"the MCP server {name!r} must be on the gateway")
    for name in allowed:
        if name in BASE_TOOLS:
            continue
        server_name = name.split("__")[1] if name.startswith("mcp__") and "__" in name[5:] else None
        if server_name not in servers:
            raise AssertionError(f"the allowed tool {name!r} is not a tool of the session")
    if options.setting_sources != []:
        raise AssertionError("a session loads no settings: setting_sources must be []")
    if options.strict_mcp_config is not True:
        raise AssertionError("a session loads no other MCP config: strict_mcp_config")
    if options.permission_mode != "bypassPermissions":
        raise AssertionError("a session runs headless: permission_mode is bypassPermissions")


def build_options(
    *,
    system_prompt: str,
    cwd: Path,
    model: str,
    effort: str | None,
    max_turns: int,
    env: dict[str, str],
    gateway: dict[str, dict[str, Any]],
    gateway_url: str,
    assignment: McpSdkServerConfig,
    resume: str | None,
) -> ClaudeAgentOptions:
    """The SDK options of one slice. ``check_session_tools`` checks them."""
    kwargs: dict[str, Any] = {
        "system_prompt": system_prompt,
        "cwd": str(cwd),
        "tools": list(BASE_TOOLS),
        "allowed_tools": [*BASE_TOOLS, *(f"mcp__{ASSIGNMENT_SERVER}__{n}" for n in TOOL_SPECS)],
        "disallowed_tools": list(FORBIDDEN_TOOLS),
        "mcp_servers": {**gateway, ASSIGNMENT_SERVER: assignment},
        "strict_mcp_config": True,
        "permission_mode": "bypassPermissions",
        "setting_sources": [],
        "max_turns": max_turns,
        "model": model,
        "env": env,
        "max_buffer_size": MAX_BUFFER_SIZE,
    }
    if effort:
        kwargs["effort"] = effort
    if resume:
        kwargs["resume"] = resume
    options = ClaudeAgentOptions(**kwargs)
    check_session_tools(options, set(gateway), gateway_url)
    return options


# --- usage -------------------------------------------------------------------

_SDK_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, int(value))


def usage_counts(raw: Any) -> dict[str, int]:
    """The four counts of the ``usage`` dict of an SDK message. A missing count is 0."""
    if not isinstance(raw, dict):
        return dict.fromkeys(_SDK_FIELDS, 0)
    return {name: _count(raw.get(key)) for name, key in _SDK_FIELDS.items()}


class UsageMeter:
    """The usage of one slice, read from its messages as they arrive.

    The result message holds the totals. Without one (an interrupt, a
    failure), the meter adds the usage of each model message, once for each
    ``message_id``.
    """

    def __init__(self) -> None:
        self._messages: dict[str, dict[str, int]] = {}
        self._result: dict[str, int] | None = None

    def note(self, msg: Any) -> None:
        kind = type(msg).__name__
        raw = getattr(msg, "usage", None)
        if kind == "ResultMessage" and isinstance(raw, dict):
            self._result = usage_counts(raw)
        elif kind == "AssistantMessage" and isinstance(raw, dict):
            key = getattr(msg, "message_id", None) or f"#{len(self._messages)}"
            self._messages[str(key)] = usage_counts(raw)

    @property
    def calls(self) -> int:
        """The model messages seen."""
        return len(self._messages)

    @property
    def usage(self) -> dict[str, int]:
        if self._result is not None:
            return dict(self._result)
        total = dict.fromkeys(_SDK_FIELDS, 0)
        for counts in self._messages.values():
            for name in total:
                total[name] += counts[name]
        return total


# --- one slice -----------------------------------------------------------------


@dataclass
class SliceOutcome:
    """What one slice gave.

    ``stop`` is why the runner stopped the slice early: ``limits`` (a
    ``RateLimitEvent`` crossed a ceiling), ``tool`` (``ask`` or ``finish``),
    or ``time`` (the working clock). None means the slice ended by itself.
    ``error`` is a short label, never the error text.
    """

    session_id: str | None = None
    usage: dict[str, int] | None = None
    calls: int = 0
    turns: int = 0
    subtype: str | None = None
    stop: str | None = None
    error: str | None = None
    rate_limit_info: Any = None


def error_label(msg: Any) -> str:
    """A short label for a failed result: ``http_<status>`` or the subtype."""
    status = getattr(msg, "api_error_status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return f"http_{status}"
    subtype = getattr(msg, "subtype", None)
    return str(subtype) if isinstance(subtype, str) and subtype else "error"


async def _bounded(awaitable: Awaitable[Any]) -> None:
    try:
        await asyncio.wait_for(awaitable, STOP_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — stopping the CLI is best effort
        logger.warning({"message": "the session did not stop cleanly", "error": type(exc).__name__})


async def run_slice(
    options: ClaudeAgentOptions,
    prompt: str,
    *,
    clock: Clock,
    over_ceiling: Callable[[Any], bool],
    stop_requested: Callable[[], bool],
    on_session_id: Callable[[str], Awaitable[None]],
    client_factory: Callable[..., Any] = ClaudeSDKClient,
    watch_s: float = WATCH_S,
    outcome: SliceOutcome | None = None,
) -> SliceOutcome:
    """Run one slice until it ends, a ceiling, a tool stop, or the clock runs out.

    ``over_ceiling`` gets the ``rate_limit_info`` of each ``RateLimitEvent``
    and returns True at a ceiling. ``stop_requested`` is True after ``ask``
    or ``finish``. ``on_session_id`` gets the session id when it is first
    known, so the row has it before the slice ends. ``outcome`` is filled
    in place, so a caller that cancels the slice still reads its usage.
    """
    outcome = outcome if outcome is not None else SliceOutcome()
    meter = UsageMeter()
    client = client_factory(options=options)

    async def consume() -> None:
        await client.connect()
        await client.query(prompt)
        async for msg in client.receive_response():
            meter.note(msg)
            kind = type(msg).__name__
            session_id = getattr(msg, "session_id", None)
            if kind == "SystemMessage":
                data = getattr(msg, "data", None)
                session_id = data.get("session_id") if isinstance(data, dict) else None
            if isinstance(session_id, str) and session_id and session_id != outcome.session_id:
                outcome.session_id = session_id
                await on_session_id(session_id)
            if kind == "RateLimitEvent":
                info = getattr(msg, "rate_limit_info", None)
                if over_ceiling(info):
                    outcome.stop = "limits"
                    outcome.rate_limit_info = info
                    return
            elif kind == "ResultMessage":
                outcome.turns = _count(getattr(msg, "num_turns", 0))
                outcome.subtype = getattr(msg, "subtype", None)
                if getattr(msg, "is_error", False) and outcome.subtype != MAX_TURNS_SUBTYPE:
                    outcome.error = error_label(msg)
            if stop_requested():
                outcome.stop = "tool"
                return

    task = asyncio.ensure_future(consume())
    try:
        while True:
            remaining = clock.remaining()
            if remaining <= 0:
                outcome.stop = "time"
                break
            done, _ = await asyncio.wait({task}, timeout=min(watch_s, remaining))
            if done:
                task.result()
                break
        if outcome.stop is not None:
            await _bounded(client.interrupt())
    except Exception as exc:  # noqa: BLE001 — every failure becomes a label
        outcome.error = f"session_{type(exc).__name__}"
        logger.warning({"message": "the session failed", "error": type(exc).__name__})
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await _bounded(client.disconnect())
        outcome.usage = meter.usage
        outcome.calls = meter.calls
        if not outcome.turns:
            outcome.turns = meter.calls
    return outcome
