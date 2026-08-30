"""MCP filtering-proxy logic.

Connects to a real upstream MCP server (stdio/http/sse) and builds a low-level MCP
``Server`` whose handlers forward every request to that upstream, but filter
``tools/list`` to an allowlist and reject a filtered-out tool at call time (the MCP
framework does not, so the gateway enforces it). A ``builtin`` upstream runs
in-process (the files MCP) over an in-memory transport, so the same filter, policy,
and call-log path applies to it. Only this module, ``upstream.py``, and
``files_mcp`` import the ``mcp`` package.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import mcp_types as types
from joshua_shared.log import get_logger
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.shared._httpx_utils import create_mcp_http_client
from pydantic import ValidationError

from joshua_gateway.catalog import ServerSpec, permits
from joshua_gateway.observability import person_ctx

log = get_logger("gateway.proxy")

OnError = Callable[[Exception], None]
OnCall = Callable[[str, float, str, str | None], None]


@asynccontextmanager
async def upstream_streams(cfg: dict[str, Any]):
    """Open read/write streams to an upstream MCP server by transport type."""
    typ = cfg.get("type", "stdio")
    if typ == "builtin":
        from joshua_gateway.files_mcp.server import build_builtin_server

        server = build_builtin_server(cfg["builtin"], cfg.get("options", {}))
        async with InMemoryTransport(server) as (read, write):
            yield read, write
    elif typ == "stdio":
        params = StdioServerParameters(
            command=cfg["command"],
            args=cfg.get("args", []),
            env=cfg.get("env") or None,
        )
        async with stdio_client(params) as (read, write):
            yield read, write
    elif typ in ("http", "streamable-http"):
        headers = cfg.get("headers")
        async with AsyncExitStack() as stack:
            http_client = None
            if headers:
                http_client = await stack.enter_async_context(
                    create_mcp_http_client(headers=headers)
                )
            read, write = await stack.enter_async_context(
                streamable_http_client(cfg["url"], http_client=http_client)
            )
            yield read, write
    elif typ == "sse":
        async with sse_client(cfg["url"], headers=cfg.get("headers")) as (read, write):
            yield read, write
    else:
        raise ValueError(f"unknown upstream type: {typ}")


def build_upstream_server(
    name: str,
    session: ClientSession,
    caps: Any,
    spec: ServerSpec,
    lock: asyncio.Lock | None = None,
    on_error: OnError | None = None,
    on_call: OnCall | None = None,
) -> Server:
    """Build a low-level ``Server`` that forwards to ``session``.

    ``tools/list`` and call-time checks run ``spec``'s tool filter and per-person
    rule against the request person (read from ``person_ctx``); a tool the person
    may not use is hidden from the list and rejected at call time. A lock (if
    given) serializes upstream calls, since the stdio transport is not safe for
    interleaved requests. ``on_error`` is called when a forwarded call raises so
    the supervisor can treat the session as broken and reconnect. ``on_call`` is
    invoked once per tool call with ``(tool, duration_ms, status, error)`` for the
    call log; status is ``"success"``, ``"error"`` or ``"denied"``.
    """

    @asynccontextmanager
    async def guard():
        try:
            if lock is not None:
                async with lock:
                    yield
            else:
                yield
        except ValidationError:
            raise  # schema mismatch — not a transport error, don't reconnect
        except Exception as exc:
            if on_error is not None:
                on_error(exc)
            raise

    async def on_list_tools(ctx, params):
        person = person_ctx.get()
        async with guard():
            tools = (await session.list_tools()).tools
        tools = [t for t in tools if permits(spec, t.name, person)]
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx, params):
        tool = params.name
        person = person_ctx.get()
        # Enforce the filter at CALL time, not just in tools/list. A consumer that
        # knows a filtered-out name could otherwise call it directly.
        if not permits(spec, tool, person):
            if on_call is not None:
                on_call(tool, 0.0, "denied", "tool not permitted")
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"tool not permitted: {tool}")],
                is_error=True,
            )
        start = time.perf_counter()
        try:
            async with guard():
                res = await session.call_tool(tool, params.arguments or {})
        except Exception as exc:
            if on_call is not None:
                on_call(tool, (time.perf_counter() - start) * 1000, "error", str(exc))
            raise
        duration_ms = (time.perf_counter() - start) * 1000
        if on_call is not None:
            status = "error" if res.is_error else "success"
            on_call(tool, duration_ms, status, "tool error" if res.is_error else None)
        return res

    async def on_list_resources(ctx, params):
        async with guard():
            return await session.list_resources()

    async def on_read_resource(ctx, params):
        async with guard():
            return await session.read_resource(str(params.uri))

    async def on_list_prompts(ctx, params):
        async with guard():
            return await session.list_prompts()

    async def on_get_prompt(ctx, params):
        async with guard():
            return await session.get_prompt(params.name, params.arguments)

    handlers: dict[str, Any] = {}
    if caps and caps.tools:
        handlers["on_list_tools"] = on_list_tools
        handlers["on_call_tool"] = on_call_tool
    if caps and caps.resources:
        handlers["on_list_resources"] = on_list_resources
        handlers["on_read_resource"] = on_read_resource
    if caps and caps.prompts:
        handlers["on_list_prompts"] = on_list_prompts
        handlers["on_get_prompt"] = on_get_prompt
    return Server(name, **handlers)
