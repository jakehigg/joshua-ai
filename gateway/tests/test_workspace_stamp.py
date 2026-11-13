"""The workspace stamp and the items of a tool call.

The upstream is a real ``MCPServer`` (FastMCP) in the gateway process, held by
the ``documents`` builtin entry through the in-memory transport. Its tools read
the request context as an addon author does, so the test proves that the
upstream sees ``_meta["joshua/workspace"]`` and nothing else sets it.
"""

from __future__ import annotations

import json

import httpx
import pytest
from conftest import gateway_session
from joshua_gateway import workspace_items
from joshua_gateway.files_mcp import server as files_server
from joshua_gateway.main import lifespan
from joshua_gateway.workspace_items import ItemRecorder
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, ResourceLink, TextContent

WORKSPACE = "11111111-1111-4111-8111-111111111111"
STAMPED = {
    "X-Joshua-Person": "alex",
    "X-Joshua-Role": "member",
    "X-Joshua-Workspace": WORKSPACE,
}
PLAIN = {"X-Joshua-Person": "alex", "X-Joshua-Role": "member"}

DOCUMENTS = "documents:\n  kind: builtin\n  allow: all\n"


def _upstream() -> MCPServer:
    app = MCPServer("documents")

    @app.tool()
    def seen_meta(ctx: Context) -> dict:
        """What an addon author reads: the workspace id in the request meta."""
        meta = ctx.request_context.meta
        return {"workspace": meta.get("joshua/workspace") if meta else None}

    @app.tool()
    def write_document(path: str, body: str, title: str | None = None) -> dict:
        """Shaped like the reply of the documents server."""
        return {"path": path, "created": True, "bytes": len(body), "kind": "note"}

    @app.tool()
    def make_links() -> CallToolResult:
        """A result with two links, one that is unsafe to open."""
        return CallToolResult(
            content=[
                TextContent(text="made"),
                ResourceLink(uri="https://shop.example/cart/1", name="Cart (3 items)"),
                ResourceLink(uri="javascript:alert(1)", name="Bad"),
            ]
        )

    @app.tool()
    def broken() -> dict:
        raise ValueError("nope")

    return app


@pytest.fixture
def posted(monkeypatch):
    """A recorder that posts to a fake core, and the list of what it got."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "i"})

    recorder = ItemRecorder("http://core", "tok", transport=httpx.MockTransport(handler))
    workspace_items.set_recorder(recorder)
    yield requests, recorder
    workspace_items.set_recorder(None)


@pytest.fixture
def app(gateway, monkeypatch):
    monkeypatch.setattr(files_server, "build_builtin_server", lambda name, options: _upstream())
    return gateway(DOCUMENTS)


def _bodies(requests: list[httpx.Request]) -> list[dict]:
    return [json.loads(r.content) for r in requests]


def _seen(result) -> str | None:
    return json.loads(result.content[0].text)["workspace"]


async def _meta_seen(app, headers) -> str | None:
    async with lifespan(app):
        async with gateway_session(app, "/documents", "core", headers) as session:
            result = await session.call_tool("seen_meta", {})
    return _seen(result)


async def test_the_upstream_sees_the_workspace_in_the_request_meta(app, posted):
    assert await _meta_seen(app, STAMPED) == WORKSPACE


async def test_a_call_outside_a_workspace_carries_no_key(app, posted):
    assert await _meta_seen(app, PLAIN) is None


async def test_the_assignments_container_is_trusted_with_the_header_too(app, posted):
    async with lifespan(app):
        async with gateway_session(app, "/documents", "assignments", STAMPED) as session:
            result = await session.call_tool("seen_meta", {})
    assert _seen(result) == WORKSPACE


async def test_a_bad_workspace_header_is_refused(app, posted):
    async with lifespan(app):
        with pytest.raises(Exception):  # noqa: B017 — the 400 ends the session start
            async with gateway_session(
                app, "/documents", "core", {**PLAIN, "X-Joshua-Workspace": "not-a-uuid"}
            ):
                pass


async def test_a_document_write_from_a_workspace_adds_an_item(app, posted):
    requests, recorder = posted
    async with lifespan(app):
        async with gateway_session(app, "/documents", "core", STAMPED) as session:
            await session.call_tool(
                "write_document",
                {"path": "documents/alex/plan.md", "body": "hi", "title": "The plan"},
            )
        await recorder.drain()
    assert [r.url.path for r in requests] == [f"/v1/workspaces/by-id/{WORKSPACE}/items"]
    assert requests[0].headers["authorization"] == "Bearer tok"
    assert _bodies(requests) == [
        {
            "kind": "document",
            "ref": "documents/alex/plan.md",
            "title": "The plan",
            "added_by": "tool:documents",
            "url": "/documents/alex/plan",
        }
    ]


async def test_an_html_document_is_an_artifact(app, posted):
    requests, recorder = posted
    async with lifespan(app):
        async with gateway_session(app, "/documents", "core", STAMPED) as session:
            await session.call_tool(
                "write_document", {"path": "documents/alex/page.html", "body": "x", "title": "P"}
            )
        await recorder.drain()
    assert _bodies(requests)[0]["kind"] == "artifact"


async def test_a_call_outside_a_workspace_adds_nothing(app, posted):
    requests, recorder = posted
    async with lifespan(app):
        async with gateway_session(app, "/documents", "core", PLAIN) as session:
            await session.call_tool(
                "write_document", {"path": "documents/alex/plan.md", "body": "hi", "title": "T"}
            )
            await session.call_tool("make_links", {})
        await recorder.drain()
    assert requests == []


async def test_resource_links_become_link_items_and_an_unsafe_address_has_no_url(app, posted):
    requests, recorder = posted
    async with lifespan(app):
        async with gateway_session(app, "/documents", "core", STAMPED) as session:
            result = await session.call_tool("make_links", {})
        await recorder.drain()
    # The result is passed on as the upstream made it.
    assert [b.type for b in result.content] == ["text", "resource_link", "resource_link"]
    assert _bodies(requests) == [
        {
            "kind": "link",
            "ref": "https://shop.example/cart/1",
            "title": "Cart (3 items)",
            "added_by": "tool:documents",
            "url": "https://shop.example/cart/1",
        },
        {
            "kind": "link",
            "ref": "javascript:alert(1)",
            "title": "Bad",
            "added_by": "tool:documents",
        },
    ]


async def test_an_unknown_tool_and_a_failed_call_add_nothing(app, posted):
    requests, recorder = posted
    async with lifespan(app):
        async with gateway_session(app, "/documents", "core", STAMPED) as session:
            await session.call_tool("seen_meta", {})
            failed = await session.call_tool("broken", {})
        await recorder.drain()
    assert failed.is_error
    assert requests == []


async def test_a_failing_core_never_fails_the_tool_call(app, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    recorder = ItemRecorder("http://core", "tok", transport=httpx.MockTransport(handler))
    workspace_items.set_recorder(recorder)
    try:
        async with lifespan(app):
            async with gateway_session(app, "/documents", "core", STAMPED) as session:
                result = await session.call_tool(
                    "write_document", {"path": "documents/alex/p.md", "body": "x", "title": "T"}
                )
            await recorder.drain()
    finally:
        workspace_items.set_recorder(None)
    assert result.is_error is False


async def test_without_core_settings_no_item_is_recorded(app, monkeypatch):
    monkeypatch.delenv("CORE_URL", raising=False)
    workspace_items.set_recorder(None)
    assert workspace_items.recorder_from_env({}) is None
    async with lifespan(app):
        async with gateway_session(app, "/documents", "core", STAMPED) as session:
            result = await session.call_tool(
                "write_document", {"path": "documents/alex/p.md", "body": "x", "title": "T"}
            )
    assert result.is_error is False


# --- the per-tool map, on result shapes ------------------------------------------------------


def _structured(payload: dict) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(text=json.dumps(payload))], structured_content=payload
    )


def _text_only(payload: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(text=json.dumps(payload))])


@pytest.mark.parametrize(
    ("server", "tool", "args", "reply", "expected"),
    [
        (
            "joshua",
            "write_page",
            {"path": "projects/solar.md", "markdown": "x"},
            {"path": "projects/solar.md", "bytes": 4, "overwritten": False, "committed": True},
            [("wiki", "projects/solar.md", "", "/wiki/projects/solar.md")],
        ),
        (
            "tasks",
            "create_project",
            {"name": "Paint the hallway"},
            {"id": "uuid-1", "name": "Paint the hallway", "slug": "paint-the-hallway"},
            [("tasks_project", "paint-the-hallway", "Paint the hallway", None)],
        ),
        (
            "developer",
            "develop",
            {"repo": "github.com/o/r", "brief": "Fix the login bug\nmore text"},
            {"task_id": "t-1", "persona": "dev", "status": "queued"},
            [("developer_task", "t-1", "Fix the login bug", None)],
        ),
        (
            "developer",
            "rework",
            {"repo": "github.com/o/r", "feedback": "Rename the flag"},
            {"task_id": "t-2", "status": "queued"},
            [("developer_task", "t-2", "Rename the flag", None)],
        ),
        (
            "assignments",
            "start_assignment",
            {"spec": {"goal": "x"}},
            {"id": 7, "status": "running", "title": "Survey"},
            [("assignment", "7", "Survey", "/assignments/7")],
        ),
        (
            "documents",
            "write_document",
            {"path": "documents/alex/x.md", "body": "b", "title": "X"},
            {"result": {"path": "documents/alex/x.md", "created": True}},
            [("document", "documents/alex/x.md", "X", "/documents/alex/x")],
        ),
    ],
)
def test_the_map_reads_each_real_result_shape(server, tool, args, reply, expected):
    for build in (_structured, _text_only):
        items = workspace_items.items_for(server, tool, args, build(reply))
        assert [(i.kind, i.ref, i.title, i.url) for i in items] == expected


def test_a_refusal_and_an_unknown_tool_add_nothing_from_the_map():
    refusal = {"status": "rejected", "reason": "busy", "message": "m"}
    assert (
        workspace_items.items_for("assignments", "start_assignment", {}, _structured(refusal)) == []
    )
    assert (
        workspace_items.items_for("developer", "list_tasks", {}, _structured({"task_id": "x"}))
        == []
    )
    assert (
        workspace_items.items_for(
            "developer", "develop", {}, CallToolResult(content=[], is_error=True)
        )
        == []
    )


def test_an_assignment_link_is_not_added_twice():
    result = CallToolResult(
        content=[
            TextContent(text="{}"),
            ResourceLink(uri="/assignments/7", name="Assignment 7"),
            ResourceLink(uri="https://example.org/r", name="Report"),
        ],
        structured_content={"id": 7, "status": "running", "title": "Survey"},
    )
    items = workspace_items.items_for("assignments", "start_assignment", {}, result)
    assert [(i.kind, i.ref) for i in items] == [
        ("assignment", "7"),
        ("link", "https://example.org/r"),
    ]


def test_the_core_address_defaults_to_the_platform_service():
    assert workspace_items.core_url({"CORE_URL": " http://c:1 "}) == "http://c:1"
    assert workspace_items.core_url({}) == "http://core:8000"
    assert workspace_items.core_url({"KUBERNETES_SERVICE_HOST": "10.0.0.1"}) == (
        "http://joshua-core:8000"
    )
    assert workspace_items.recorder_from_env({}) is None
    assert workspace_items.recorder_from_env({"JOSHUA_TOKEN_GATEWAY": "t"}) is not None
