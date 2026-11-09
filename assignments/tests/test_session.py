"""The session boundary: the tools, the MCP servers, the environment, and one slice."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest
from claude_agent_sdk.types import TextBlock, ToolResultBlock, UserMessage
from joshua_assignments import session
from joshua_assignments.clock import Clock
from joshua_shared.gateway_mcp import gateway_servers
from sdk_fakes import FakeSdk, assistant, rate_limit, result, tool_error

GATEWAY = "http://gateway:8000"


async def _noop(args):
    return {"content": []}


def handlers():
    return dict.fromkeys(session.TOOL_SPECS, _noop)


def options(**changes):
    gateway = gateway_servers(["files", "gitlab"], GATEWAY, "tok", "alex", "assignment:1", "member")
    built = session.build_options(
        system_prompt="x",
        cwd=Path("/work/1"),
        model="claude-sonnet-5",
        effort="medium",
        max_turns=10,
        env={},
        gateway=gateway,
        gateway_url=GATEWAY,
        assignment=session.assignment_server(handlers()),
        resume=None,
    )
    return dataclasses.replace(built, **changes) if changes else built


def test_the_built_options_pass_the_check() -> None:
    built = options()
    assert built.tools == list(session.BASE_TOOLS)
    assert set(built.disallowed_tools) == {"WebSearch", "WebFetch"}
    assert built.setting_sources == [] and built.permission_mode == "bypassPermissions"
    assert set(built.mcp_servers) == {"files", "gitlab", "assignment"}
    session.check_session_tools(built, {"files", "gitlab"}, GATEWAY)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"tools": [*session.BASE_TOOLS, "WebFetch"]}, "only the file tools"),
        ({"tools": ["Read", "Task"]}, "only the file tools"),
        ({"tools": None}, "only the file tools"),
        ({"allowed_tools": ["WebSearch"]}, "WebSearch or WebFetch"),
        ({"disallowed_tools": ["WebSearch"]}, "must refuse"),
        ({"setting_sources": ["project"]}, "loads no settings"),
        ({"strict_mcp_config": False}, "no other MCP config"),
        ({"permission_mode": "default"}, "headless"),
        ({"allowed_tools": ["mcp__other__x"]}, "not a tool of the session"),
    ],
)
def test_a_session_outside_the_boundary_fails(changes, message) -> None:
    with pytest.raises(AssertionError, match=message):
        session.check_session_tools(options(**changes), {"files", "gitlab"}, GATEWAY)


def test_an_mcp_server_outside_the_persons_list_fails() -> None:
    built = options()
    with pytest.raises(AssertionError, match="not in the person's gateway list"):
        session.check_session_tools(built, {"files"}, GATEWAY)
    extra = {**built.mcp_servers, "assignments": {"type": "http", "url": f"{GATEWAY}/assignments"}}
    with pytest.raises(AssertionError, match="assignments server"):
        session.check_session_tools(
            dataclasses.replace(built, mcp_servers=extra),
            {"files", "gitlab", "assignments"},
            GATEWAY,
        )
    with pytest.raises(AssertionError, match="must be a map"):
        session.check_session_tools(dataclasses.replace(built, mcp_servers="x"), set(), GATEWAY)


def test_a_server_must_be_on_the_gateway_and_the_assignment_server_ours() -> None:
    built = options()
    elsewhere = {**built.mcp_servers, "gitlab": {"type": "http", "url": "http://evil.example/x"}}
    with pytest.raises(AssertionError, match="on the gateway"):
        session.check_session_tools(
            dataclasses.replace(built, mcp_servers=elsewhere), {"files", "gitlab"}, GATEWAY
        )
    stdio = {**built.mcp_servers, "gitlab": {"type": "stdio", "command": "sh"}}
    with pytest.raises(AssertionError, match="HTTP server"):
        session.check_session_tools(
            dataclasses.replace(built, mcp_servers=stdio), {"files", "gitlab"}, GATEWAY
        )
    forged = {**built.mcp_servers, "assignment": {"type": "sdk", "name": "assignment"}}
    with pytest.raises(AssertionError, match="assignment_server"):
        session.check_session_tools(
            dataclasses.replace(built, mcp_servers=forged), {"files", "gitlab"}, GATEWAY
        )


def test_build_options_refuses_a_bad_gateway_map() -> None:
    with pytest.raises(AssertionError):
        session.build_options(
            system_prompt="x",
            cwd=Path("/w"),
            model="m",
            effort=None,
            max_turns=1,
            env={},
            gateway={"assignments": {"type": "http", "url": f"{GATEWAY}/assignments"}},
            gateway_url=GATEWAY,
            assignment=session.assignment_server(handlers()),
            resume="s",
        )


def test_the_assignment_server_needs_the_four_tools() -> None:
    with pytest.raises(ValueError, match="the tools"):
        session.assignment_server({"checkpoint": _noop})
    assert session.server_handlers({"type": "sdk"}) is None
    assert session.server_handlers("x") is None
    assert session.server_handlers({"instance": []}) is None


def test_the_session_env_hides_the_container_secrets() -> None:
    env = session.session_env(
        {
            "PATH": "/bin",
            "DATABASE_URL": "postgresql://u:pw@db/x",
            "ADDON_TOKEN": "a",
            "JOSHUA_TOKEN_VIEWER": "v",
        },
        core_url="http://core:8000/",
        token="fleet",
        home=Path("/work/1/.home"),
        config_dir=Path("/work/1/.home/.claude"),
    )
    assert env["DATABASE_URL"] == "" and env["ADDON_TOKEN"] == ""
    assert env["JOSHUA_TOKEN_VIEWER"] == ""
    assert "PATH" not in env
    assert env["ANTHROPIC_BASE_URL"] == "http://core:8000/v1/model"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "fleet"
    for name, value in session.QUIET_ENV.items():
        assert env[name] == value


def test_the_usage_meter_counts_each_message_once() -> None:
    meter = session.UsageMeter()
    meter.note(assistant("a"))
    meter.note(assistant("a"))
    meter.note(assistant("b", usage={"input_tokens": 1, "output_tokens": True}))
    meter.note(object())
    assert meter.calls == 2
    assert meter.usage == {"input": 101, "output": 20, "cache_read": 1000, "cache_write": 5}
    meter.note(result(usage={"input_tokens": 7}))
    assert meter.usage == {"input": 7, "output": 0, "cache_read": 0, "cache_write": 0}
    assert session.usage_counts(None) == dict.fromkeys(meter.usage, 0)


def test_the_error_label_is_never_the_text() -> None:
    assert session.error_label(result("error_during_execution", api_error_status=429)) == "http_429"
    assert session.error_label(result("error_during_execution")) == "error_during_execution"
    assert session.error_label(object()) == "error"


async def test_run_slice_reads_a_slice_and_keeps_the_session_id() -> None:
    seen: list[str] = []

    async def keep(session_id: str) -> None:
        seen.append(session_id)

    sdk = FakeSdk([assistant("a"), rate_limit(0.2), result("success", turns=3)])
    outcome = await session.run_slice(
        options(),
        "go",
        clock=Clock(100),
        over_ceiling=lambda info: info.utilization > 0.8,
        stop_requested=lambda: False,
        on_session_id=keep,
        client_factory=sdk,
        watch_s=0.01,
    )
    assert seen == ["sess-1"]
    assert (outcome.turns, outcome.subtype, outcome.stop, outcome.error) == (
        3,
        "success",
        None,
        None,
    )
    assert sdk.clients[0].interrupted is False and sdk.clients[0].disconnected is True


def test_tool_errors_read_only_the_failed_results() -> None:
    msg = UserMessage(
        content=[
            ToolResultBlock(tool_use_id="a", content="fine", is_error=False),
            ToolResultBlock(tool_use_id="b", content="  exit 128  ", is_error=True),
            ToolResultBlock(
                tool_use_id="c",
                content=[{"type": "text", "text": "502"}, {"type": "image"}, "x"],
                is_error=True,
            ),
            ToolResultBlock(tool_use_id="d", content=None, is_error=True),
            TextBlock(text="not a result"),
        ]
    )
    assert session.tool_errors(msg) == ["exit 128", "502"]
    assert session.tool_errors(UserMessage(content="plain text")) == []
    long = session.tool_errors(tool_error("e" * 5000))
    assert len(long[0]) == session.MAX_TOOL_ERROR


async def test_run_slice_keeps_the_last_tool_errors() -> None:
    async def keep(session_id: str) -> None:
        return None

    steps = [tool_error(f"error {i}") for i in range(15)]
    sdk = FakeSdk([*steps, result("success")])
    outcome = await session.run_slice(
        options(),
        "go",
        clock=Clock(100),
        over_ceiling=lambda info: False,
        stop_requested=lambda: False,
        on_session_id=keep,
        client_factory=sdk,
        watch_s=0.01,
    )
    assert outcome.tool_errors == [f"error {i}" for i in range(5, 15)]


async def test_run_slice_with_no_time_left_interrupts_at_once() -> None:
    async def keep(session_id: str) -> None:
        return None

    sdk = FakeSdk(["hang"])
    outcome = await session.run_slice(
        options(),
        "go",
        clock=Clock(0),
        over_ceiling=lambda info: False,
        stop_requested=lambda: False,
        on_session_id=keep,
        client_factory=sdk,
    )
    assert outcome.stop == "time"
    assert sdk.clients[0].interrupted is True


async def test_a_stop_that_hangs_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(session, "STOP_TIMEOUT_S", 0.01)

    async def forever() -> None:
        await asyncio.Event().wait()

    await session._bounded(forever())
