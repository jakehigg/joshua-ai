"""Exercise the SDK adapter with a fake client so the run loop, the readiness
gate, and ``run_oneshot`` run without the real SDK."""

from __future__ import annotations

import pytest
from joshua_core.engine import agent


class FakeOptions:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class StreamEvent:
    def __init__(self, text: str):
        self.event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}


class Block:
    def __init__(self, *, text=None, name=None, type=None, input=None):
        self.text = text
        self.name = name
        self.type = type
        self.input = input


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self, *, result, session_id, is_error=False):
        self.result = result
        self.session_id = session_id
        self.usage = {"input_tokens": 1, "output_tokens": 2}
        self.total_cost_usd = 0.01
        self.is_error = is_error


class FakeClient:
    def __init__(self, options):
        self.options = options
        self.queries: list[str] = []
        self.interrupted = False
        self.disconnected = False

    async def connect(self):
        return None

    async def get_mcp_status(self):
        return {"mcpServers": [{"name": "weather", "status": "connected"}]}

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        yield StreamEvent("Hello ")
        yield AssistantMessage([Block(name="mcp__weather__get", type="tool_use", input={"q": "x"})])
        yield ResultMessage(result="Hello world", session_id="sess-9")

    async def interrupt(self):
        self.interrupted = True

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.setattr(agent, "ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr(agent, "ClaudeSDKClient", FakeClient)


async def test_run_collects_result_and_tools(sdk):
    options = agent.build_options(
        system_prompt="sys",
        cwd="/tmp/x",
        mcp_servers={"weather": {"type": "http", "url": "http://gw/weather"}},
        model="claude-opus-5",
        max_turns=5,
        resume=None,
    )
    session = agent.AgentSession(options)
    deltas: list[str] = []

    result = await session.run("hi", on_delta=_collect(deltas))

    assert result.text == "Hello world"
    assert result.session_id == "sess-9"
    assert result.tools_used == ["mcp__weather__get"]
    assert result.tool_calls[0]["input"] == {"q": "x"}
    assert deltas == ["Hello "]
    assert session.connected

    await session.interrupt()
    await session.close()
    assert not session.connected


async def test_run_oneshot_returns_text(sdk):
    text = await agent.run_oneshot(system_prompt="sys", user_prompt="hi", model=None)
    assert text == "Hello world"


def test_remote_server_names_only_http_sse():
    servers = {
        "weather": {"type": "http"},
        "events": {"type": "sse"},
        "scheduling": {"type": "sdk"},
    }
    assert agent._remote_server_names(servers) == {"weather", "events"}


def _collect(sink):
    async def on_delta(chunk: str) -> None:
        sink.append(chunk)

    return on_delta


# -- Tool results ride on a user message (#26) -------------------------------


class _Block:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Msg:
    def __init__(self, content):
        self.content = content


def test_tool_results_are_read_from_a_user_message() -> None:
    """A result carries the id of the call it answers, and whether it failed."""
    msg = _Msg(
        [
            _Block(tool_use_id="a", is_error=False),
            _Block(tool_use_id="b", is_error=True),
        ]
    )
    assert agent._tool_results(msg) == {"a": False, "b": True}


def test_a_tool_call_carries_its_id() -> None:
    """Without the id a result cannot be joined to the call that made it."""
    msg = _Msg(
        [_Block(type="tool_use", name="mcp__files__write_file", input={"path": "x"}, id="a")]
    )
    calls = agent._tool_calls(msg)
    assert calls == [{"id": "a", "name": "mcp__files__write_file", "input": {"path": "x"}}]


def test_tool_results_ignores_a_message_with_no_blocks() -> None:
    assert agent._tool_results(_Msg("plain text")) == {}
