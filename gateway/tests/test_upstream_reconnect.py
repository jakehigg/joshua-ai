"""An http upstream that loses its session: reconnect, and send the call once more.

The fake upstream is a real stateful MCP server behind a small ASGI wrapper.
The wrapper answers ``404 Session not found`` to the next ``tools/call`` when a
test asks it to, the way an upstream that restarted or expired the session
does. The gateway reaches it in-process: ``create_mcp_http_client`` is patched
to return a client on an ASGI transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys

import httpx2
import mcp_types as types
import pytest
from conftest import FIXTURE, TOKENS, bearer, gateway_session
from joshua_gateway import proxy
from joshua_gateway.main import build_app, lifespan
from joshua_gateway.proxy import UpstreamUnavailableError
from joshua_gateway.upstream import CLOSED, REJECTED, Upstream, session_error_kind
from joshua_shared import config
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError

UPSTREAM_URL = "http://upstream.test/mcp"

HTTP_MCP = f"""\
health:
  type: http
  url: {UPSTREAM_URL}
  allow: all
"""

SESSION_NOT_FOUND = json.dumps(
    {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Session not found"}}
).encode()


class FakeUpstream:
    """A stateful MCP server that can refuse its session on the next calls."""

    def __init__(self) -> None:
        self.ran: list[str] = []  # each tool call the server ran
        self.reject_calls = 0  # how many tools/call to refuse with 404
        self.fail_after_run = 0  # how many tools/call to run, then answer 500
        self.calls_seen = 0  # every tools/call that reached the wrapper

        async def on_list_tools(ctx, params):
            return types.ListToolsResult(
                tools=[types.Tool(name="add", input_schema={"type": "object"})]
            )

        async def on_call_tool(ctx, params):
            self.ran.append(params.name)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"ran {len(self.ran)}")]
            )

        server = Server("fake", on_list_tools=on_list_tools, on_call_tool=on_call_tool)
        self.mgr = StreamableHTTPSessionManager(app=server, json_response=True)

    async def __call__(self, scope, receive, send) -> None:
        if scope["method"] == "GET":
            # No server-to-client stream; the spec allows 405 here.
            await _respond(send, 405, b"")
            return
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        method = json.loads(body).get("method") if body else None
        if method == "tools/call":
            self.calls_seen += 1
            if self.reject_calls > 0:
                self.reject_calls -= 1
                await _respond(send, 404, SESSION_NOT_FOUND, b"application/json")
                return
        replay = _replay(body)
        if method == "tools/call" and self.fail_after_run > 0:
            self.fail_after_run -= 1
            await self.mgr.handle_request(scope, replay, _discard)
            await _respond(send, 500, b"upstream broke after the call")
            return
        await self.mgr.handle_request(scope, replay, send)


def _replay(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


async def _discard(message) -> None:
    return None


async def _respond(send, status: int, body: bytes, ctype: bytes = b"text/plain") -> None:
    await send(
        {"type": "http.response.start", "status": status, "headers": [(b"content-type", ctype)]}
    )
    await send({"type": "http.response.body", "body": body})


@pytest.fixture
async def fake(monkeypatch):
    upstream = FakeUpstream()

    def client(headers=None, timeout=None, auth=None):
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=upstream),
            base_url="http://upstream.test",
            headers=headers,
            timeout=30,
        )

    monkeypatch.setattr(proxy, "create_mcp_http_client", client)
    # The manager's task group must close in the task that opened it, and a
    # fixture tears down in another task, so the manager runs in its own task.
    ready, stop = asyncio.Event(), asyncio.Event()

    async def host() -> None:
        async with upstream.mgr.run():
            ready.set()
            await stop.wait()

    task = asyncio.create_task(host())
    await ready.wait()
    yield upstream
    stop.set()
    await task


@contextlib.asynccontextmanager
async def running(gateway, mcp: str = HTTP_MCP):
    app = gateway(mcp)
    async with lifespan(app):
        yield app


def reconnect_lines(caplog) -> list[dict]:
    return [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("message") == "upstream reconnecting"
    ]


async def _inventory(app) -> dict:
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return (await client.get("/admin/inventory", headers=bearer("laptop"))).json()


# -- the error classes ---------------------------------------------------------


def test_session_not_found_is_a_rejected_session():
    exc = MCPError(code=types.INVALID_REQUEST, message="Session not found")
    assert session_error_kind(exc) == REJECTED
    expired = MCPError(code=-32001, message="Bad Request: unknown or expired session ID")
    assert session_error_kind(expired) == REJECTED


def test_a_closed_connection_is_a_closed_session():
    assert session_error_kind(MCPError(code=types.CONNECTION_CLOSED, message="x")) == CLOSED


def test_a_tool_error_from_a_live_session_is_not_a_session_error():
    exc = MCPError(code=types.INVALID_PARAMS, message="bad arguments")
    assert session_error_kind(exc) is None
    assert session_error_kind(RuntimeError("boom")) is None


# -- the retry -----------------------------------------------------------------


async def test_an_expired_session_reconnects_and_sends_the_call_once_more(fake, gateway, caplog):
    with caplog.at_level(logging.INFO, logger="gateway.upstream"):
        async with running(gateway) as app:
            async with gateway_session(app, "/health", "core") as session:
                first = await session.call_tool("add", {})
                assert first.is_error is False

                fake.reject_calls = 1
                second = await session.call_tool("add", {})

                # The same downstream session goes on after the reconnect.
                third = await session.call_tool("add", {})
            up: Upstream = app.state.upstreams["health"]
            inventory = await _inventory(app)

    assert second.is_error is False, second.content
    assert second.content[0].text == "ran 2"
    assert third.is_error is False
    assert fake.ran == ["add", "add", "add"], "the refused call ran once, on the retry"
    lines = reconnect_lines(caplog)
    assert len(lines) == 1
    assert lines[0]["server"] == "health"
    assert "Session not found" in lines[0]["reason"]
    assert up.reconnects == 1

    entry = inventory["servers"]["health"]
    assert entry["reconnects"] == 1
    assert entry["last_reconnect_at"]
    assert entry["instances"][0]["reconnects"] == 1
    assert entry["instances"][0]["last_reconnect_at"] == entry["last_reconnect_at"]


async def test_a_second_failure_is_a_tool_error_and_no_loop(fake, gateway, caplog):
    with caplog.at_level(logging.INFO, logger="gateway.upstream"):
        async with running(gateway) as app:
            async with gateway_session(app, "/health", "core") as session:
                fake.reject_calls = 2
                result = await session.call_tool("add", {})
                after = await session.call_tool("add", {})

    assert result.is_error is True
    assert result.content[0].text == "upstream health is reconnecting; try again"
    assert fake.calls_seen == 3, "two attempts for the failed call, one for the next"
    assert fake.ran == ["add"], "only the call after the failure ran"
    assert after.is_error is False, "the session works again after the failure"


async def test_a_call_that_failed_after_it_ran_is_not_sent_twice(fake, gateway, caplog):
    with caplog.at_level(logging.INFO, logger="gateway.upstream"):
        async with running(gateway) as app:
            async with gateway_session(app, "/health", "core") as session:
                fake.fail_after_run = 1
                result = await session.call_tool("add", {})

    assert result.is_error is True
    assert result.content[0].text.startswith("upstream health failed the call")
    assert fake.ran == ["add"]
    assert fake.calls_seen == 1
    assert reconnect_lines(caplog) == [], "an answer from a live session is not a lost session"


async def test_a_closed_transport_does_not_repeat_a_tool_call(fake, gateway):
    async with running(gateway) as app:
        up: Upstream = app.state.upstreams["health"]
        sent: list[int] = []

        async def call(session):
            sent.append(1)
            raise MCPError(code=types.CONNECTION_CLOSED, message="Connection closed")

        with pytest.raises(UpstreamUnavailableError, match="may have run"):
            await up.forward(call, repeatable=False)
        assert sent == [1]
        assert await up.wait_connected(10.0)


async def test_a_closed_transport_repeats_a_list(fake, gateway):
    async with running(gateway) as app:
        up: Upstream = app.state.upstreams["health"]
        sent: list[int] = []

        async def call(session):
            sent.append(1)
            if len(sent) == 1:
                raise MCPError(code=types.CONNECTION_CLOSED, message="Connection closed")
            return (await session.list_tools()).tools

        tools = await up.forward(call, repeatable=True)
    assert [t.name for t in tools] == ["add"]
    assert sent == [1, 1]


# -- the downstream session during a reconnect ---------------------------------


async def test_a_request_during_a_reconnect_waits_and_is_not_400_or_503(fake, gateway):
    statuses: list[int] = []

    async with running(gateway) as app:

        async def watched(scope, receive, send):
            async def spy(message):
                if message["type"] == "http.response.start":
                    statuses.append(message["status"])
                await send(message)

            await app(scope, receive, spy)

        headers = bearer("core")
        client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=watched),
            base_url="http://testserver",
            headers=headers,
            timeout=30,
        )
        up: Upstream = app.state.upstreams["health"]
        try:
            async with streamable_http_client("http://testserver/health", http_client=client) as (
                read,
                write,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    up.notify_broken(RuntimeError("upstream went away"))
                    assert not up.connected and up.serving
                    tools = (await session.list_tools()).tools
                    result = await session.call_tool("add", {})
        finally:
            await client.aclose()

    assert [t.name for t in tools] == ["add"]
    assert result.is_error is False
    assert up.reconnects == 1
    assert 400 not in statuses and 503 not in statuses, statuses


async def test_a_down_upstream_that_is_not_reconnecting_is_still_503(fake, gateway):
    async with running(gateway) as app:
        up: Upstream = app.state.upstreams["health"]
        up._drop_session()  # down, and no reconnect in progress
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as c:
            resp = await c.post("/health", headers=bearer("core"), json={})
    assert resp.status_code == 503


# -- identity connections reconnect on their own -------------------------------


def _identities_yaml() -> str:
    people = "".join(f"  - id: {p}\n    name: {p.title()}\n" for p in ("alex", "mia"))
    return (
        "name: Test\n"
        "timezone: America/New_York\n"
        "people:\n"
        f"{people}"
        "mcp:\n"
        "  spotify:\n"
        "    type: stdio\n"
        f"    command: {sys.executable}\n"
        "    args:\n"
        f"      - {FIXTURE}\n"
        "    allow:\n      - alex\n      - mia\n"
        "    tools:\n      allow:\n        - whoami\n"
        "    identities:\n"
        "      alex:\n        env:\n          ECHO_IDENTITY: alex-value\n"
        "      mia:\n        env:\n          ECHO_IDENTITY: mia-value\n"
    )


async def test_identity_connections_reconnect_one_at_a_time(monkeypatch, config_path, caplog):
    config_path.write_text(_identities_yaml())
    monkeypatch.setattr(config, "_cache", None)
    for identity, token in TOKENS.items():
        monkeypatch.setenv(f"JOSHUA_TOKEN_{identity.upper()}", token)
    app = build_app()
    with caplog.at_level(logging.INFO, logger="gateway.upstream"):
        async with lifespan(app):
            alex: Upstream = app.state.upstreams["spotify:alex"]
            mia: Upstream = app.state.upstreams["spotify:mia"]
            mia_session = mia.session

            alex.notify_broken(RuntimeError("session gone"))
            assert await alex.wait_connected(30.0)

            assert alex.reconnects == 1
            assert mia.reconnects == 0
            assert mia.session is mia_session, "mia's connection was not touched"
            async with gateway_session(
                app, "/spotify", "core", {"X-Joshua-Person": "alex"}
            ) as session:
                assert (await session.call_tool("whoami", {})).content[0].text == "alex-value"
            inventory = await _inventory(app)

    assert [line["server"] for line in reconnect_lines(caplog)] == ["spotify:alex"]
    by_person = {i["person"]: i for i in inventory["servers"]["spotify"]["instances"]}
    assert by_person["alex"]["reconnects"] == 1
    assert by_person["mia"]["reconnects"] == 0
    assert by_person["mia"]["last_reconnect_at"] is None
