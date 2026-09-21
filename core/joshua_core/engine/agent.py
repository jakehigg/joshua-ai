"""Thin adapter over the Claude Agent SDK.

This is the ONLY module that imports ``claude_agent_sdk``. Everything else speaks
to the engine through ``AgentSession`` (one live SDK client per conversation) and
``TurnResult``. The import is guarded so stub mode and the offline test run load
this module without the SDK installed; the SDK-backed paths stay unused there.

The SDK spawns the bundled Claude Code CLI and authenticates with
``CLAUDE_CODE_OAUTH_TOKEN`` from the environment.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from joshua_shared.log import get_logger

from joshua_core.engine.types import OnDelta, TurnResult

try:  # The SDK is absent in stub mode and in the offline test run.
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        HookMatcher,
        create_sdk_mcp_server,
        tool,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised only without the SDK
    ClaudeAgentOptions = None  # type: ignore[assignment,misc]
    ClaudeSDKClient = None  # type: ignore[assignment,misc]
    HookMatcher = None  # type: ignore[assignment,misc]
    create_sdk_mcp_server = None  # type: ignore[assignment]
    tool = None  # type: ignore[assignment]

logger = get_logger("engine.agent")

# Ceiling for ONE newline-framed JSON message on the CLI's stdout. The SDK reads
# stdout as newline-framed JSON, defaults to a 1MiB cap, and treats an overrun as
# fatal to the reader task — which kills the turn and poisons the pooled client.
# 32MiB bounds a single oversized frame (a big tool result) while keeping memory
# per client bounded (defense in depth).
_MAX_BUFFER_SIZE = 32 * 1024 * 1024

# The agent has NO SDK built-in tools. Every capability is an MCP server (gateway
# or in-process builtin). ``build_options`` hard-codes this list and asserts the
# built options carry it; there is no parameter to widen it (decided 2026-08-25
# core has no direct web egress).
_NO_TOOLS: list[str] = []

# The only built-in tools a worker may hold, and the only place in this repo
# where a tool list is not empty. ``WebSearch`` runs on Anthropic's side.
# ``WebFetch`` runs here, so it is gated by a PreToolUse hook over a block
# list; see ``joshua_shared.netblock``. A worker still gets no file tool, no
# shell, and no MCP server.
WORKER_TOOLS: frozenset[str] = frozenset({"WebSearch", "WebFetch"})

__all__ = [
    "AgentSession",
    "TurnResult",
    "build_options",
    "create_sdk_mcp_server",
    "build_worker_options",
    "run_oneshot",
    "run_structured",
    "run_worker_session",
    "tool",
]

# --- MCP readiness gate ----------------------------------------------------
# The CLI connects MCP servers in the background at connect() time. If the first
# turn queries the model before a network-backed server finishes its handshake,
# that server's tools are absent from context. We close the race by polling the
# CLI's live MCP status after connect() and holding the first turn until the
# gateway-backed servers reach a terminal state.
_MCP_TERMINAL_STATES = frozenset({"connected", "failed", "needs-auth", "disabled"})
# Only remote transports race the first turn; in-process SDK servers connect
# synchronously, so the gate does not wait on them.
_MCP_REMOTE_TYPES = frozenset({"http", "sse"})
_MCP_READY_POLL_S = 0.2


def _remote_server_names(mcp_servers: Any) -> set[str]:
    """Names of the network-backed (http/sse) MCP servers whose connection can
    lag the first turn."""
    names: set[str] = set()
    if isinstance(mcp_servers, dict):
        for name, cfg in mcp_servers.items():
            if isinstance(cfg, dict) and cfg.get("type") in _MCP_REMOTE_TYPES:
                names.add(name)
    return names


def build_options(
    *,
    system_prompt: str,
    cwd: Path,
    mcp_servers: dict[str, Any],
    model: str | None,
    max_turns: int,
    resume: str | None,
    output_format: dict[str, Any] | None = None,
) -> Any:
    """Construct ``ClaudeAgentOptions`` for one conversation.

    ``system_prompt`` is a plain string, so it fully replaces the Claude Code
    preset. ``setting_sources=[]`` loads no filesystem CLAUDE.md or settings, so
    the composed prompt is the whole instruction surface. ``permission_mode``
    bypass runs tools without an interactive prompt (this is a headless service).
    ``tools`` is the empty list: the agent gets no SDK built-in tools.
    """
    kwargs: dict[str, Any] = dict(
        system_prompt=system_prompt,
        cwd=str(cwd),
        mcp_servers=mcp_servers,
        tools=list(_NO_TOOLS),
        permission_mode="bypassPermissions",
        setting_sources=[],
        max_turns=max_turns,
        include_partial_messages=True,
        max_buffer_size=_MAX_BUFFER_SIZE,
    )
    if model:
        kwargs["model"] = model
    if resume:
        kwargs["resume"] = resume
    if output_format:
        kwargs["output_format"] = output_format
    options = ClaudeAgentOptions(**kwargs)
    assert options.tools == [], "core agent must expose no SDK built-in tools"
    return options


def build_worker_options(
    *,
    system_prompt: str,
    cwd: Path,
    model: str | None,
    max_turns: int,
    tools: list[str],
    output_format: dict[str, Any] | None = None,
    hooks: dict[str, Any] | None = None,
) -> Any:
    """Construct ``ClaudeAgentOptions`` for one ephemeral worker.

    A worker is not the agent. It holds one objective, no conversation, and no
    MCP server, so it may hold a built-in tool that the agent may not: a
    ``research`` worker searches the web. The assert below is the boundary. It
    names what a worker may hold, so a file tool, a shell, or anything else
    fails here rather than in production.
    """
    unknown = sorted(set(tools) - WORKER_TOOLS)
    assert not unknown, f"a worker may not hold these tools: {unknown}"

    kwargs: dict[str, Any] = dict(
        system_prompt=system_prompt,
        cwd=str(cwd),
        mcp_servers={},
        tools=list(tools),
        allowed_tools=list(tools),
        permission_mode="bypassPermissions",
        setting_sources=[],
        max_turns=max_turns,
        max_buffer_size=_MAX_BUFFER_SIZE,
    )
    if model:
        kwargs["model"] = model
    if output_format:
        kwargs["output_format"] = output_format
    if hooks:
        kwargs["hooks"] = _hook_matchers(hooks)
    options = ClaudeAgentOptions(**kwargs)
    assert set(options.tools) <= WORKER_TOOLS, "a worker may hold only the web tools"
    assert options.mcp_servers == {}, "a worker reaches no MCP server"
    return options


def _hook_matchers(hooks: dict[str, Any]) -> dict[str, Any]:
    """Wrap ``{event: [callback]}`` in the ``HookMatcher`` the SDK expects.

    The SDK takes a list of matchers for each event, not a list of functions. A
    bare function is accepted and then never called, which is silent and, for a
    hook that guards a fetch, dangerous. Callers pass plain functions and this
    is the one place that knows the SDK's shape.
    """
    wrapped: dict[str, Any] = {}
    for event, callbacks in hooks.items():
        matchers = []
        for callback in callbacks:
            if HookMatcher is not None and isinstance(callback, HookMatcher):
                matchers.append(callback)
            else:
                matchers.append(HookMatcher(hooks=[callback]))
        wrapped[event] = matchers
    return wrapped


async def run_worker_session(
    *,
    system_prompt: str,
    user_text: str,
    schema: dict[str, Any],
    model: str | None,
    timeout_s: float,
    max_turns: int,
    tools: list[str],
    hooks: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Run one worker that may use a tool, and answer with JSON matching ``schema``.

    Unlike ``run_structured``, this worker takes several turns: it searches, it
    reads, and then it answers. It still holds no conversation and no MCP
    server. Returns the structured answer, or None on a failure or a timeout.
    """
    cwd = Path(tempfile.mkdtemp(prefix="joshua-worker-"))
    options = build_worker_options(
        system_prompt=system_prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        tools=tools,
        output_format={"type": "json_schema", "schema": schema},
        hooks=hooks,
    )
    client = ClaudeSDKClient(options=options)
    try:
        return await asyncio.wait_for(_worker_turn(client, user_text), timeout=timeout_s)
    except TimeoutError:
        logger.warning({"message": "worker timed out", "timeout_s": timeout_s})
        return None
    except Exception as exc:  # noqa: BLE001 — a worker failure never breaks the turn
        logger.warning({"message": "worker failed", "error": str(exc)})
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 — teardown is best effort
            pass
        shutil.rmtree(cwd, ignore_errors=True)


async def _worker_turn(client: Any, user_text: str) -> dict[str, Any] | None:
    """Send the objective, let the worker use its tools, return the answer."""
    await client.connect()
    await client.query(user_text)
    async for msg in client.receive_response():
        for block in getattr(msg, "content", None) or []:
            name = getattr(block, "name", None)
            if name:
                logger.info({"message": "worker tool", "tool": str(name)})
        structured = getattr(msg, "structured_output", None)
        if isinstance(structured, dict):
            return structured
        result = getattr(msg, "result", None)
        if isinstance(result, str) and result.strip().startswith("{"):
            try:
                parsed = json.loads(result)
            except ValueError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


async def run_oneshot(*, system_prompt: str, user_prompt: str, model: str | None) -> str:
    """One transient, tool-less turn — for cheap side calls such as the nightly
    reflection pass. Spawns a fresh CLI subprocess in an empty scratch dir, runs a
    single query, and tears down. Returns the result text ("" on failure)."""
    cwd = Path(tempfile.mkdtemp(prefix="joshua-oneshot-"))
    options = build_options(
        system_prompt=system_prompt,
        cwd=cwd,
        mcp_servers={},
        model=model,
        max_turns=1,
        resume=None,
    )
    session = AgentSession(options)
    try:
        result = await session.run(user_prompt)
        return result.text or ""
    finally:
        await session.close()
        shutil.rmtree(cwd, ignore_errors=True)


async def run_structured(
    *,
    system_prompt: str,
    user_text: str,
    schema: dict[str, Any],
    model: str | None,
    timeout_s: float,
    image: tuple[bytes, str] | None = None,
    document: bytes | None = None,
) -> dict[str, Any] | None:
    """Run one tool-less turn that answers with JSON matching ``schema``.

    The turn carries ``user_text`` and, when given, one image (bytes and media
    type) or one PDF. There is no conversation history: the CLI starts in an
    empty directory, runs one query, and is torn down. Returns the structured
    answer, or None when the call fails or passes ``timeout_s``.

    This is the only way a worker reaches the model. The options come from
    ``build_options``, so the tool list is empty here too.
    """
    cwd = Path(tempfile.mkdtemp(prefix="joshua-worker-"))
    options = build_options(
        system_prompt=system_prompt,
        cwd=cwd,
        mcp_servers={},
        model=model,
        max_turns=1,
        resume=None,
        output_format={"type": "json_schema", "schema": schema},
    )
    client = ClaudeSDKClient(options=options)
    try:
        return await asyncio.wait_for(
            _structured_turn(client, user_text, image, document), timeout=timeout_s
        )
    except TimeoutError:
        logger.warning({"message": "worker timed out", "timeout_s": timeout_s})
        return None
    except Exception as exc:  # noqa: BLE001 — a worker failure never breaks the turn
        logger.warning({"message": "worker failed", "error": str(exc)})
        return None
    finally:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 — teardown is best effort
            pass
        shutil.rmtree(cwd, ignore_errors=True)


async def _structured_turn(
    client: Any,
    user_text: str,
    image: tuple[bytes, str] | None,
    document: bytes | None,
) -> dict[str, Any] | None:
    """Send one user message with its file and return the structured answer."""
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if image is not None:
        data, media_type = image
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            }
        )
    elif document is not None:
        content.append(
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(document).decode("ascii"),
                },
            }
        )

    async def _one_message() -> Any:
        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
            "session_id": "default",
        }

    await client.connect()
    await client.query(_one_message())
    async for msg in client.receive_response():
        structured = getattr(msg, "structured_output", None)
        if isinstance(structured, dict):
            return structured
        result = getattr(msg, "result", None)
        if isinstance(result, str) and result.strip().startswith("{"):
            try:
                parsed = json.loads(result)
            except ValueError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def _extract_text_delta(msg: Any) -> str | None:
    """Pull a streamed text delta out of a partial-message event, if present."""
    event = getattr(msg, "event", None)
    if isinstance(event, dict) and event.get("type") == "content_block_delta":
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            return delta.get("text") or None
    return None


def _assistant_text(msg: Any) -> str:
    parts: list[str] = []
    for block in getattr(msg, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


def _tool_calls(msg: Any) -> list[dict[str, Any]]:
    """Collect tool_use calls (id + name + input) from an assistant message.

    Match on either the ``tool_use`` type or a bare ``name`` so a minor SDK shape
    change degrades to "no tools captured" rather than an error. The ``id`` ties
    a call to its result; see ``_tool_results``.
    """
    calls: list[dict[str, Any]] = []
    for block in getattr(msg, "content", None) or []:
        btype = getattr(block, "type", None)
        name = getattr(block, "name", None)
        if name and (btype == "tool_use" or btype is None) and not getattr(block, "text", None):
            inp = getattr(block, "input", None)
            calls.append(
                {
                    "id": str(getattr(block, "id", "") or ""),
                    "name": str(name),
                    "input": inp if isinstance(inp, dict) else {},
                }
            )
    return calls


def _tool_results(msg: Any) -> dict[str, bool]:
    """Map each ``tool_use_id`` in a user message to whether it failed.

    A tool result comes back on a ``UserMessage``, not on the assistant message
    that made the call, so a caller joins the two on the id. Without the join, a
    refused write reads the same as a write that landed.
    """
    results: dict[str, bool] = {}
    content = getattr(msg, "content", None)
    if not isinstance(content, list):
        return results
    for block in content:
        use_id = getattr(block, "tool_use_id", None)
        if use_id:
            results[str(use_id)] = bool(getattr(block, "is_error", False))
    return results


class AgentSession:
    """One long-lived SDK client bound to one conversation.

    A warm client keeps its CLI subprocess and MCP connections alive between
    turns — the latency win over re-spawning per turn.
    """

    def __init__(self, options: Any, ready_timeout_s: float = 12.0):
        self._options = options
        self._client: Any = None
        # Gateway-backed servers to wait on before the first turn (see connect()).
        self._remote_servers = _remote_server_names(getattr(options, "mcp_servers", None))
        self._ready_timeout_s = ready_timeout_s

    @property
    def connected(self) -> bool:
        return self._client is not None

    async def connect(self) -> None:
        client = ClaudeSDKClient(options=self._options)
        await client.connect()
        self._client = client
        await self._await_mcp_ready()

    async def _await_mcp_ready(self) -> None:
        """Hold until every gateway-backed MCP server reaches a terminal status or
        the timeout elapses. Best-effort: any failure to read status just proceeds,
        so the gate can never wedge a turn."""
        if not self._remote_servers or self._ready_timeout_s <= 0 or self._client is None:
            return
        deadline = time.monotonic() + self._ready_timeout_s
        started = time.monotonic()
        servers: dict[str, str] = {}
        while True:
            try:
                status = await self._client.get_mcp_status()
            except Exception as e:  # noqa: BLE001 — status is best-effort; degrade to no gate
                logger.warning(
                    {"message": "mcp readiness check unavailable; proceeding", "error": str(e)}
                )
                return
            servers = {
                str(s.get("name")): str(s.get("status"))
                for s in (status or {}).get("mcpServers", [])
            }
            pending = [
                n for n in self._remote_servers if servers.get(n) not in _MCP_TERMINAL_STATES
            ]
            if not pending:
                break
            if time.monotonic() >= deadline:
                logger.warning(
                    {
                        "message": "mcp servers not ready before first turn; proceeding",
                        "pending": sorted(pending),
                        "waited_ms": int((time.monotonic() - started) * 1000),
                    }
                )
                return
            await asyncio.sleep(_MCP_READY_POLL_S)
        failed = {n: servers.get(n) for n in self._remote_servers if servers.get(n) != "connected"}
        if failed:
            logger.warning({"message": "mcp servers ready with failures", "not_connected": failed})
        else:
            logger.info({"message": "mcp servers ready", "count": len(self._remote_servers)})

    async def run(self, prompt: str, on_delta: OnDelta | None = None) -> TurnResult:
        t_start = time.monotonic()
        cold_start = self._client is None
        if cold_start:
            await self.connect()
        assert self._client is not None
        connect_ms = round((time.monotonic() - t_start) * 1000)

        t_query = time.monotonic()
        await self._client.query(prompt)

        streamed = False
        parts: list[str] = []
        calls: list[dict[str, Any]] = []
        session_id: str | None = None
        usage: dict[str, Any] | None = None
        cost: float | None = None
        is_error = False
        tool_errors: dict[str, bool] = {}
        result_text: str | None = None

        async for msg in self._client.receive_response():
            delta = _extract_text_delta(msg)
            if delta is not None:
                streamed = True
                parts.append(delta)
                if on_delta is not None:
                    await on_delta(delta)
                continue

            cls = type(msg).__name__
            if cls == "UserMessage":
                # A tool result rides on a user message. Record the outcome, so
                # a refused write is not reported as a write.
                tool_errors.update(_tool_results(msg))
            elif cls == "AssistantMessage":
                calls.extend(_tool_calls(msg))
                # With partial streaming the assembled message duplicates the
                # deltas; use it only as a fallback.
                if not streamed:
                    text = _assistant_text(msg)
                    if text:
                        parts.append(text)
                        if on_delta is not None:
                            await on_delta(text)
            elif cls == "ResultMessage":
                session_id = getattr(msg, "session_id", None)
                usage = getattr(msg, "usage", None)
                cost = getattr(msg, "total_cost_usd", None)
                is_error = bool(getattr(msg, "is_error", False))
                result_text = getattr(msg, "result", None)

        # Deliver the final result (the clean final answer), not the concatenation
        # of all assistant text, which includes inter-tool narration.
        text = (result_text or "").strip() or "".join(parts).strip()
        # Stamp each call with its outcome. A call with no result recorded is
        # treated as failed: silence is not success, and over-reporting a write
        # is the fault this guards.
        for call in calls:
            call["ok"] = not tool_errors.get(str(call.get("id") or ""), True)
        seen: set[str] = set()
        tools_used = [c["name"] for c in calls if not (c["name"] in seen or seen.add(c["name"]))]
        logger.info(
            {
                "message": "sdk turn timing",
                "cold_start": cold_start,
                "connect_ms": connect_ms if cold_start else 0,
                "query_ms": round((time.monotonic() - t_query) * 1000),
                "call_count": len(calls),
            }
        )
        return TurnResult(
            text=text,
            session_id=session_id,
            is_error=is_error,
            usage=usage,
            cost_usd=cost,
            tools_used=tools_used,
            tool_calls=calls,
        )

    async def interrupt(self) -> None:
        if self._client is not None:
            try:
                await self._client.interrupt()
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.warning({"message": "interrupt failed", "error": str(e)})

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:  # noqa: BLE001 — best-effort
                logger.warning({"message": "disconnect failed", "error": str(e)})
            finally:
                self._client = None
