"""The ``limits`` tool server: its one tool, its summary, which jobs get it,
and the ``RateLimitEvent`` of a turn reaching the tracker only from a call on
the subscription."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from engine_fakes import (
    FakeComposer,
    FakeRepo,
    FakeSDKClient,
    FakeSDKOptions,
    install_fake_sdk,
    make_channel,
    make_conversation,
    make_settings,
)
from joshua_core import limits
from joshua_core.engine import agent
from joshua_core.engine.manager import ConversationManager
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools import limits as limits_tool
from joshua_core.limits import LimitsTracker
from joshua_shared.contracts import LimitsResponse
from test_limits import RAW_EVENT, TOKEN, Clock, Upstream, event, ok

TZ = "America/New_York"


@pytest.fixture(autouse=True)
def _no_installed_tracker():
    limits.install(None)
    yield
    limits.install(None)


def _tracker(*responses: Any, token: str = TOKEN) -> LimitsTracker:
    upstream = Upstream(*(responses or (ok(),)))
    return LimitsTracker(
        token=lambda: token, timezone=TZ, max_age_s=300, clock=Clock(), transport=upstream.transport
    )


def _deps() -> ToolDeps:
    return ToolDeps(
        repo=FakeRepo(),  # type: ignore[arg-type]
        conversation=make_conversation(),
        channel=make_channel(),
        tz=TZ,
    )


# --- the tool ---------------------------------------------------------------------


def test_the_server_has_one_tool_with_no_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_server(*, name: str, version: str, tools: list[Any]) -> dict[str, Any]:
        captured.update({t.name: t for t in tools})
        return {"type": "sdk", "name": name}

    monkeypatch.setattr(limits_tool, "create_sdk_mcp_server", fake_server)
    server = limits_tool.build_limits_server(_deps(), _tracker())
    assert server["name"] == "limits"
    assert set(captured) == {"check_limits"}
    schema = captured["check_limits"].input_schema
    assert schema["properties"] == {}
    assert "required" not in schema


async def test_the_tool_gives_a_summary_and_the_numbers(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_server(*, name: str, version: str, tools: list[Any]) -> dict[str, Any]:
        captured.update({t.name: t for t in tools})
        return {"type": "sdk", "name": name}

    monkeypatch.setattr(limits_tool, "create_sdk_mcp_server", fake_server)
    limits_tool.build_limits_server(_deps(), _tracker())
    reply = await captured["check_limits"].handler({})
    text = reply["content"][0]["text"]
    summary, _, numbers = text.partition("\n\nNumbers: ")
    assert "77% of the 5-hour limit is left (23% used). It resets Sat 7 Nov, 10:30 pm." in summary
    assert "58.5% of the weekly limit is left" in summary
    assert "87% of the weekly Fable limit is left." in summary
    assert "Extra usage: 1.25 of 25 USD used this month." in summary
    assert f"Times are in {TZ}." in summary
    data = json.loads(numbers)
    assert data["five_hour"]["left_pct"] == 77.0
    assert TOKEN not in text


def test_the_summary_says_why_it_knows_nothing() -> None:
    text = limits_tool.summarize(
        LimitsResponse(available=False, reason="no subscription token"), TZ
    )
    assert text == "The subscription limits are not known: no subscription token."
    text = limits_tool.summarize(
        LimitsResponse(available=False, reason="x", stale=True, retry_after=60), TZ
    )
    assert "tries again in 60 seconds" in text


def test_the_summary_marks_stale_numbers() -> None:
    answer = LimitsResponse(available=True, source="usage_api", stale=True)
    text = limits_tool.summarize(answer, TZ)
    assert "Anthropic gave no limit numbers." in text
    assert "out of date" in text


async def test_the_tool_without_a_token_says_so() -> None:
    text = await limits_tool.do_check_limits(_tracker(token=""), TZ)
    assert text.startswith("The subscription limits are not known: no subscription token.")


# --- which jobs get the tool -------------------------------------------------------


def _voice_profile(person: Any, channel: Any) -> Any:
    return SimpleNamespace(name="voice-dm", job="voice", max_turns=0)


def _chat_profile(person: Any, channel: Any) -> Any:
    return SimpleNamespace(name="default", max_turns=0)


async def _servers(tmp_path, monkeypatch, *, block: str, profile: Any) -> set[str]:
    install_fake_sdk(monkeypatch)
    settings = make_settings(mcp=block)
    manager = ConversationManager(
        repo=FakeRepo(),
        settings=settings,
        composer=FakeComposer(),
        derive_profile=profile,
        gateway_url="http://gw:8000",
        gateway_token="tok",
        agent_backend="sdk",
        data_dir=tmp_path,
        builtin_factories={"history": lambda deps: {"kind": "history"}},
    )
    limits_tool.register(manager, _tracker(), settings)
    built = await manager._build_session(make_channel(), make_conversation(), resume=None)
    return set(built.session._options.mcp_servers)


@pytest.mark.parametrize("profile", [_chat_profile, _voice_profile])
async def test_chat_and_voice_get_the_tool_by_default(tmp_path, monkeypatch, profile) -> None:
    servers = await _servers(tmp_path, monkeypatch, block="", profile=profile)
    assert "limits" in servers
    assert "history" in servers


async def test_chat_tool_off_takes_it_from_chat_only(tmp_path, monkeypatch) -> None:
    block = "\nlimits:\n  chat_tool: false\n"
    assert "limits" not in await _servers(tmp_path, monkeypatch, block=block, profile=_chat_profile)
    assert "limits" in await _servers(tmp_path, monkeypatch, block=block, profile=_voice_profile)


async def test_voice_tool_off_takes_it_from_voice_only(tmp_path, monkeypatch) -> None:
    block = "\nlimits:\n  voice_tool: false\n"
    assert "limits" in await _servers(tmp_path, monkeypatch, block=block, profile=_chat_profile)
    servers = await _servers(tmp_path, monkeypatch, block=block, profile=_voice_profile)
    assert "limits" not in servers
    assert "history" in servers


@pytest.mark.parametrize("profile", [_chat_profile, _voice_profile])
async def test_limits_off_gives_the_tool_to_no_job(tmp_path, monkeypatch, profile) -> None:
    block = "\nlimits:\n  enabled: false\n"
    assert "limits" not in await _servers(tmp_path, monkeypatch, block=block, profile=profile)


def test_no_tracker_registers_nothing() -> None:
    wired: list[str] = []
    manager = SimpleNamespace(register_builtin=lambda name, factory, jobs=None: wired.append(name))
    limits_tool.register(manager, None, make_settings())
    assert wired == []


# --- the RateLimitEvent of a turn --------------------------------------------------


class _LimitClient(FakeSDKClient):
    async def receive_response(self):
        yield event(RAW_EVENT)
        async for msg in super().receive_response():
            yield msg


async def _turn(monkeypatch, env: dict[str, str]) -> LimitsTracker:
    monkeypatch.setattr(agent, "ClaudeSDKClient", _LimitClient)
    tracker = _tracker()
    limits.install(tracker)
    session = agent.AgentSession(FakeSDKOptions(env=env, mcp_servers={}))
    result = await session.run("hi")
    assert result.text == "Hello world"
    return tracker


async def test_a_turn_on_the_subscription_feeds_the_tracker(monkeypatch) -> None:
    tracker = await _turn(monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN})
    assert tracker.snapshot is not None
    assert tracker.snapshot.five_hour is not None
    assert tracker.snapshot.five_hour.used_pct == 31.0


async def test_a_turn_on_another_server_does_not(monkeypatch) -> None:
    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "ANTHROPIC_BASE_URL": "http://llm.example.test",
        "ANTHROPIC_AUTH_TOKEN": "k",
    }
    tracker = await _turn(monkeypatch, env)
    assert tracker.snapshot is None
