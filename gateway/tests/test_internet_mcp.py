"""The internet MCP: the one tool a session gets to read the open web.

Core is a fake here: a transport that answers ``POST /v1/internet`` with the
text a lookup gave. What these tests check is the hand-off — the question, the
URLs, the person, and the conversation go to core, and the answer comes back to
the caller as text. The lookup itself is core's (``core/tests/test_internet_api.py``).
"""

from __future__ import annotations

import json
import textwrap
from typing import Any

import httpx
import pytest
from conftest import gateway_session, joshua_yaml
from joshua_gateway.internet_mcp import server as internet_server
from joshua_gateway.internet_mcp.server import Core
from joshua_gateway.main import lifespan
from joshua_shared import config
from joshua_shared.contracts import INTERNET_PATH

ALEX = {
    "X-Joshua-Person": "alex",
    "X-Joshua-Role": "member",
    "X-Joshua-Conversation": "assignment:12",
}

# The text core gives back: the answer of the internet agent, with its sources.
ANSWER = (
    "<internet_result>\nThe valve ships with a 1/2 inch thread.\n\n"
    "Sources:\n- The valve — https://example.com/valve\n</internet_result>"
)


class FakeCore:
    """Core's internet route, over a transport. ``asked`` holds each body."""

    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status = status
        self.body = {"text": ANSWER} if body is None else body
        self.asked: list[dict[str, Any]] = []
        self.tokens: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == INTERNET_PATH
        self.asked.append(json.loads(request.content))
        self.tokens.append(request.headers.get("Authorization", ""))
        return httpx.Response(self.status, json=self.body)

    def core(self) -> Core:
        return Core(
            "http://core.test:8000",
            "gateway-token",
            10.0,
            transport=httpx.MockTransport(self.handle),
        )


def text_of(result: Any) -> str:
    return result.content[0].text


def internet_yaml() -> str:
    return textwrap.dedent("""\
        internet:
          kind: builtin
          allow: all
        """)


@pytest.fixture
def internet_app(gateway, config_path, monkeypatch):
    """A gateway app with the internet builtin, on a fake core."""

    def _make(fake: FakeCore | None = None) -> Any:
        app = gateway(internet_yaml())
        config_path.write_text(joshua_yaml(internet_yaml()))
        monkeypatch.setattr(config, "_cache", None)
        core = None if fake is None else fake.core()
        monkeypatch.setattr(internet_server, "core_from_env", lambda env, timeout_s: core)
        return app

    return _make


async def test_a_session_gets_the_one_tool(internet_app):
    app = internet_app(FakeCore())
    async with lifespan(app):
        async with gateway_session(app, "/internet", "assignments", ALEX) as session:
            tools = await session.list_tools()
    assert [tool.name for tool in tools.tools] == ["use_internet"]


async def test_the_page_the_agent_read_comes_back_as_text(internet_app):
    fake = FakeCore()
    app = internet_app(fake)
    async with lifespan(app):
        async with gateway_session(app, "/internet", "assignments", ALEX) as session:
            result = await session.call_tool(
                "use_internet",
                {
                    "question": "what thread does the valve have?",
                    "urls": ["https://example.com/valve"],
                },
            )
    assert result.is_error is False, text_of(result)
    assert "1/2 inch thread" in text_of(result)
    assert "https://example.com/valve" in text_of(result)
    assert fake.asked == [
        {
            "question": "what thread does the valve have?",
            "urls": ["https://example.com/valve"],
            "person": "alex",
            "conversation": "assignment:12",
        }
    ]
    assert fake.tokens == ["Bearer gateway-token"]


async def test_a_call_without_a_question_is_refused_and_core_is_not_asked(internet_app):
    fake = FakeCore()
    app = internet_app(fake)
    async with lifespan(app):
        async with gateway_session(app, "/internet", "assignments", ALEX) as session:
            result = await session.call_tool("use_internet", {"question": "  "})
    assert result.is_error is True
    assert "question is required" in text_of(result)
    assert fake.asked == []


async def test_with_no_token_the_tool_says_it_cannot_look_anything_up(internet_app):
    app = internet_app()
    async with lifespan(app):
        async with gateway_session(app, "/internet", "assignments", ALEX) as session:
            tools = await session.list_tools()
            result = await session.call_tool("use_internet", {"question": "what thread?"})
    # The tool is listed either way, so a session is never promised research and
    # then given no tool.
    assert [tool.name for tool in tools.tools] == ["use_internet"]
    assert "JOSHUA_TOKEN_GATEWAY" in text_of(result)


async def test_a_core_that_refuses_is_one_line_and_never_a_guess(internet_app):
    fake = FakeCore(status=503)
    app = internet_app(fake)
    async with lifespan(app):
        async with gateway_session(app, "/internet", "assignments", ALEX) as session:
            result = await session.call_tool("use_internet", {"question": "what thread?"})
    assert "not reachable" in text_of(result)


async def test_a_core_answer_in_the_wrong_shape_is_one_line(internet_app):
    fake = FakeCore(body={"answer": "no text key"})
    app = internet_app(fake)
    async with lifespan(app):
        async with gateway_session(app, "/internet", "assignments", ALEX) as session:
            result = await session.call_tool("use_internet", {"question": "what thread?"})
    assert "not reachable" in text_of(result)


async def test_core_that_cannot_be_reached_is_one_line(internet_app):
    class Down(FakeCore):
        def handle(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

    app = internet_app(Down())
    async with lifespan(app):
        async with gateway_session(app, "/internet", "assignments", ALEX) as session:
            result = await session.call_tool("use_internet", {"question": "what thread?"})
    assert "not reachable" in text_of(result)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (None, []),
        ("https://example.com/a", []),
        (["https://example.com/a", "", "  "], ["https://example.com/a"]),
        ([f"https://example.com/{i}" for i in range(20)], None),
    ],
)
def test_the_urls_of_a_call_are_a_short_list_of_strings(given, expected):
    urls = internet_server._urls(given)
    if expected is None:
        assert len(urls) == 12
    else:
        assert urls == expected
