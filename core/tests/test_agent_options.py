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
