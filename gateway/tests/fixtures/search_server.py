"""A stdio MCP server for the web search route tests.

Each tool answers a query in one shape: ``search_list`` (a JSON list, with the
arguments it got in the first snippet), ``search_nested`` (a JSON object with
the list under ``web.results``), ``search_parts`` (one JSON object for each
text part), ``search_structured`` (structured content), ``search_text``
(plain text blocks), ``search_garbage`` (text with no URL), ``search_fail``
(a tool error), ``search_slow`` (sleeps past a short timeout), and
``search_wipe`` (declared destructive). Run it over stdio:
``python search_server.py``.
"""

from __future__ import annotations

import json

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

_QUERY = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "q": {"type": "string"},
        "count": {"type": "integer"},
        "safe": {"type": "boolean"},
        "lang": {"type": "string"},
    },
}

NAMES = [
    "search_list",
    "search_nested",
    "search_parts",
    "search_structured",
    "search_text",
    "search_garbage",
    "search_fail",
    "search_slow",
]

TOOLS = [types.Tool(name=n, description=n, input_schema=_QUERY) for n in NAMES] + [
    types.Tool(
        name="search_wipe",
        description="Declared destructive.",
        input_schema=_QUERY,
        annotations=types.ToolAnnotations(destructive_hint=True),
    )
]

TEXT = """Title: Braised short ribs
URL: https://food.example/short-ribs
Description: Braise at 160 C for three hours.

Title: Short rib basics
URL: https://cook.example/basics
Description: The cut, the fat, and the time. See https://other.example/in-text too.

Title: Not a web page
URL: ftp://files.example/ribs.txt
"""


def _text(*parts: str, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=p) for p in parts], is_error=is_error
    )


async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=TOOLS)


async def on_call_tool(ctx, params):
    args = params.arguments or {}
    query = str(args.get("query", args.get("q", "")))
    name = params.name
    if name == "search_list":
        items = [
            {"title": "Args", "url": "https://args.example/", "snippet": json.dumps(args)},
            {"name": "Second", "link": "https://two.example/page", "description": query},
            {"title": "Mail", "url": "mailto:someone@example.com"},
            {"title": "Script", "url": "javascript:alert(1)"},
            {"title": "Again", "url": "https://two.example/page"},
            {"title": "Long", "href": "https://three.example/", "content": "x" * 5000},
        ]
        return _text(json.dumps(items))
    if name == "search_nested":
        body = {
            "query": query,
            "web": {"results": [{"title": "Nested", "url": "https://nested.example/a"}]},
        }
        return _text(json.dumps(body))
    if name == "search_parts":
        return _text(
            json.dumps({"title": "One", "url": "https://one.example/"}),
            json.dumps({"title": "Two", "url": "https://two.example/"}),
        )
    if name == "search_structured":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="see the structured content")],
            structured_content={"results": [{"title": "S", "url": "https://s.example/"}]},
        )
    if name == "search_text":
        return _text(TEXT)
    if name == "search_garbage":
        return _text("Nothing found for this query. {not json")
    if name == "search_fail":
        return _text("provider refused the request: PROVIDER-SECRET-VALUE", is_error=True)
    if name == "search_slow":
        await anyio.sleep(5)
        return _text("[]")
    return _text("[]")


async def main() -> None:
    server = Server("search", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
