"""A stdio MCP server that reports its own environment and working directory.

The isolation test starts it through the gateway and reads back what the child
process can see. It exposes ``dump_env`` (the environment variable names, one per
line) and ``dump_cwd`` (the working directory).
"""

from __future__ import annotations

import os
import sys

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

TOOLS = [
    types.Tool(
        name="dump_env",
        description="Return the names of every environment variable this process has.",
        input_schema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="dump_cwd",
        description="Return the working directory this process was started in.",
        input_schema={"type": "object", "properties": {}},
    ),
]


async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=TOOLS)


async def on_call_tool(ctx, params):
    if params.name == "dump_env":
        text = "\n".join(sorted(os.environ))
    elif params.name == "dump_cwd":
        text = os.getcwd()
    else:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"unknown tool: {params.name}")],
            is_error=True,
        )
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


async def main() -> None:
    # One stderr line, so the test can prove the child's stderr reaches the log.
    print("env_server started", file=sys.stderr, flush=True)
    server = Server("env", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
