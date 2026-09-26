"""Unit tests for the SDK adapter's option builder.

They run without the real SDK: a fake ``ClaudeAgentOptions`` is patched into the
module so the offline suite exercises ``build_options`` and its hard guard that
the agent carries no built-in tools.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_core.engine import agent


class FakeOptions:
    """Minimal stand-in for ``ClaudeAgentOptions`` — stores kwargs as attributes."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture
def fake_sdk(monkeypatch):
    monkeypatch.setattr(agent, "ClaudeAgentOptions", FakeOptions)


def _build(**overrides):
    kwargs = dict(
        system_prompt="you are joshua",
        cwd=Path("/data/inbox/sessions/c1"),
        mcp_servers={},
        model=None,
        max_turns=20,
        resume=None,
    )
    kwargs.update(overrides)
    return agent.build_options(**kwargs)


def test_tools_is_empty(fake_sdk):
    options = _build()
    assert options.tools == []


def test_tools_empty_for_every_input(fake_sdk):
    servers = {"weather": {"type": "http", "url": "http://gw/weather"}}
    for model in (None, "claude-opus-5"):
        for resume in (None, "sess-123"):
            options = _build(mcp_servers=servers, model=model, max_turns=5, resume=resume)
            assert options.tools == []


def test_fixed_options(fake_sdk):
    options = _build(model="claude-opus-5", resume="sess-1")
    assert options.permission_mode == "bypassPermissions"
    assert options.setting_sources == []
    assert options.include_partial_messages is True
    assert options.max_buffer_size == agent._MAX_BUFFER_SIZE
    assert options.cwd == "/data/inbox/sessions/c1"
    assert options.model == "claude-opus-5"
    assert options.resume == "sess-1"


def test_model_and_resume_omitted_when_absent(fake_sdk):
    options = _build(model=None, resume=None)
    assert not hasattr(options, "model")
    assert not hasattr(options, "resume")


def test_assertion_fires_when_tools_widened(fake_sdk, monkeypatch):
    # Any attempt to hand the agent a built-in tool must trip the module guard.
    monkeypatch.setattr(agent, "_NO_TOOLS", ["Bash"])
    with pytest.raises(AssertionError):
        _build()


# ── the worker options ───────────────────────────────────────────────────────


def test_a_worker_asks_for_a_schema_and_still_gets_no_tools(fake_sdk):
    """A worker answers with an object, and it reaches no tool to do it."""
    options = _build(
        output_format={"type": "json_schema", "schema": {"type": "object"}},
        max_turns=1,
    )
    assert options.tools == []
    assert options.output_format == {"type": "json_schema", "schema": {"type": "object"}}
    assert options.max_turns == 1


def test_a_turn_with_no_output_format_sets_none(fake_sdk):
    options = _build()
    assert not hasattr(options, "output_format")


def test_a_worker_carries_no_mcp_server_and_no_session(fake_sdk):
    """No server, and no resume: the worker sees nothing of the conversation."""
    options = _build(
        output_format={"type": "json_schema", "schema": {}}, mcp_servers={}, resume=None
    )
    assert options.mcp_servers == {}
    assert not hasattr(options, "resume")
    assert options.setting_sources == []


# ── the message a worker sends ───────────────────────────────────────────────


class FakeClient:
    """Stands in for the SDK client: records the message, returns one result."""

    def __init__(self, result):
        self._result = result
        self.sent: list[dict] = []
        self.connected = False

    async def connect(self):
        self.connected = True

    async def query(self, prompt):
        async for message in prompt:
            self.sent.append(message)

    async def receive_response(self):
        yield self._result


class FakeResult:
    def __init__(self, structured=None, result=None):
        self.structured_output = structured
        self.result = result


async def test_a_picture_is_sent_as_an_image_block():
    """The bytes reach the model as base64, with the type the file has."""
    import base64

    client = FakeClient(FakeResult(structured={"kind": "receipt"}))

    answer = await agent._structured_turn(
        client, "Describe this file.", (b"bytes", "image/png"), None
    )

    assert answer == {"kind": "receipt"}
    content = client.sent[0]["message"]["content"]
    assert content[0] == {"type": "text", "text": "Describe this file."}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/png"
    assert base64.b64decode(content[1]["source"]["data"]) == b"bytes"
    assert client.sent[0]["message"]["role"] == "user"


async def test_a_pdf_is_sent_as_a_document_block():
    client = FakeClient(FakeResult(structured={"kind": "bill"}))

    await agent._structured_turn(client, "Describe this file.", None, b"%PDF-1.7")

    content = client.sent[0]["message"]["content"]
    assert content[1]["type"] == "document"
    assert content[1]["source"]["media_type"] == "application/pdf"


async def test_one_message_and_no_history_is_sent():
    client = FakeClient(FakeResult(structured={}))

    await agent._structured_turn(client, "Describe this file.", None, None)

    assert len(client.sent) == 1
    assert client.sent[0]["parent_tool_use_id"] is None


async def test_a_json_answer_in_the_text_is_read():
    """A model that answers in text, not in the structured field, still counts."""
    client = FakeClient(FakeResult(result='{"kind": "photo"}'))

    assert await agent._structured_turn(client, "u", None, None) == {"kind": "photo"}


async def test_an_answer_that_is_not_json_gives_none():
    client = FakeClient(FakeResult(result="I am sorry, I cannot do that."))

    assert await agent._structured_turn(client, "u", None, None) is None


# ── the worker boundary ──────────────────────────────────────────────────────


def _worker(**overrides):
    kwargs = dict(
        system_prompt="look up one question",
        cwd=Path("/tmp/worker"),
        model="claude-sonnet-5",
        max_turns=12,
        tools=["WebSearch"],
    )
    kwargs.update(overrides)
    return agent.build_worker_options(**kwargs)


def test_a_worker_may_hold_the_web_tools(fake_sdk):
    options = _worker(tools=["WebSearch", "WebFetch"])
    assert options.tools == ["WebSearch", "WebFetch"]
    assert options.allowed_tools == ["WebSearch", "WebFetch"]


def test_a_worker_reaches_no_mcp_server_and_no_settings(fake_sdk):
    """It holds one objective. Nothing of this instance reaches it."""
    options = _worker()
    assert options.mcp_servers == {}
    assert options.setting_sources == []
    assert not hasattr(options, "resume")


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "Task"])
def test_a_worker_with_a_file_or_shell_tool_fails_to_build(fake_sdk, tool):
    """The assert is the boundary: a file tool never reaches a worker."""
    with pytest.raises(AssertionError, match="may not hold"):
        _worker(tools=["WebSearch", tool])


def test_the_agent_itself_still_gets_no_tool_at_all(fake_sdk):
    """The worker's tools change nothing about the agent."""
    assert _build().tools == []


def test_a_worker_carries_its_hook(fake_sdk):
    async def hook(payload, tool_use_id, context):
        return {}

    options = _worker(tools=["WebSearch", "WebFetch"], hooks={"PreToolUse": [hook]})

    assert "PreToolUse" in options.hooks


def test_a_hook_is_wrapped_in_the_matcher_the_sdk_expects(fake_sdk):
    """A bare function is accepted by the SDK and then never called.

    That is silent, and for the hook that guards a fetch it means every URL is
    allowed. Measured once against the real SDK; pinned here.
    """
    from claude_agent_sdk import HookMatcher

    async def hook(payload, tool_use_id, context):
        return {}

    options = _worker(tools=["WebSearch", "WebFetch"], hooks={"PreToolUse": [hook]})

    matchers = options.hooks["PreToolUse"]
    assert matchers and all(isinstance(m, HookMatcher) for m in matchers)
    assert matchers[0].hooks == [hook]


def test_a_matcher_that_is_already_built_is_left_alone(fake_sdk):
    from claude_agent_sdk import HookMatcher

    async def hook(payload, tool_use_id, context):
        return {}

    matcher = HookMatcher(matcher="WebFetch", hooks=[hook])
    options = _worker(tools=["WebFetch"], hooks={"PreToolUse": [matcher]})

    assert options.hooks["PreToolUse"] == [matcher]
