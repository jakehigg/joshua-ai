"""A stdio MCP server for the snapshot route tests.

Tools: ``list_items`` (two text parts, and the ``SNAP_IDENTITY`` env var in the
second), ``fail_items`` (a tool error), ``slow_items`` (sleeps past a short
timeout), ``wipe_items`` (declared destructive), ``safe_wipe`` (declared
destructive and read-only), and ``add_item``. Run it over stdio:
``python snapshot_server.py``.
"""

from __future__ import annotations

import os

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

_EMPTY = {"type": "object", "properties": {}}

TOOLS = [
    types.Tool(name="list_items", description="List the items.", input_schema=_EMPTY),
    types.Tool(name="fail_items", description="Fail.", input_schema=_EMPTY),
    types.Tool(name="slow_items", description="Answer slowly.", input_schema=_EMPTY),
    types.Tool(
        name="wipe_items",
        description="Delete every item.",
        input_schema=_EMPTY,
        annotations=types.ToolAnnotations(destructive_hint=True),
    ),
    types.Tool(
        name="safe_wipe",
        description="Declared destructive and read-only.",
        input_schema=_EMPTY,
        annotations=types.ToolAnnotations(destructive_hint=True, read_only_hint=True),
    ),
    types.Tool(name="add_item", description="Add an item.", input_schema=_EMPTY),
]


def _text(*parts: str, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=p) for p in parts], is_error=is_error
    )


async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=TOOLS)


async def on_call_tool(ctx, params):
    args = params.arguments or {}
    if params.name == "list_items":
        return _text(f"items for {args.get('list', 'none')}", os.environ.get("SNAP_IDENTITY", ""))
    if params.name == "fail_items":
        return _text("the list is not available", is_error=True)
    if params.name == "slow_items":
        await anyio.sleep(5)
        return _text("late")
    if params.name == "safe_wipe":
        return _text("nothing wiped")
    return _text(f"ran {params.name}")


async def main() -> None:
    server = Server("snap", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
