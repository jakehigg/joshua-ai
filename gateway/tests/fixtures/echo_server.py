"""A minimal stdio MCP server for the gateway tests.

It exposes three tools: ``echo`` (returns its ``message``), ``secret_tool`` (the
allowlist tests keep it out of ``tools/list`` and reject it at call time), and
``whoami`` (returns the ``ECHO_IDENTITY`` env var, so the identities tests can see
which per-person credential the instance was started with). Run it over stdio:
``python echo_server.py``.
"""

from __future__ import annotations

import os

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

TOOLS = [
    types.Tool(
        name="echo",
        description="Return the message.",
        input_schema={
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    ),
    types.Tool(
        name="secret_tool",
        description="Must not be exposed through the gateway allowlist.",
        input_schema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="whoami",
        description="Return the ECHO_IDENTITY env var this server was started with.",
        input_schema={"type": "object", "properties": {}},
    ),
]


async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=TOOLS)


async def on_call_tool(ctx, params):
    if params.name == "echo":
        message = (params.arguments or {}).get("message", "")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"echo: {message}")]
        )
    if params.name == "secret_tool":
        return types.CallToolResult(content=[types.TextContent(type="text", text="secret")])
    if params.name == "whoami":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=os.environ.get("ECHO_IDENTITY", ""))]
        )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"unknown tool: {params.name}")],
        is_error=True,
    )


async def main() -> None:
    server = Server("echo", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
