"""A session whose client marked a gateway server failed is replaced, once.

The Claude Agent SDK stops sending to an MCP server that it marked ``failed``,
for as long as the client lives. A warm session can live for hours, so after
the turn the manager marks the session, and the next turn gets a new client.
"""

from __future__ import annotations

from engine_fakes import (
    FakeComposer,
    FakeRepo,
    FakeSDKClient,
    fake_derive_profile,
    install_fake_sdk,
    make_channel,
    make_conversation,
    make_settings,
)
from joshua_core.engine import agent, manager
from joshua_core.engine.agent import AgentSession
from joshua_core.engine.manager import ConversationManager

GATEWAY_MCP = """
mcp:
  health:
    type: http
    url: http://health.example/mcp
    allow: all
"""


class FailedHealthClient(FakeSDKClient):
    """An SDK client that reports the gateway ``health`` server as failed."""

    async def get_mcp_status(self):
        return {"mcpServers": [{"name": "health", "status": "failed"}]}


def make_manager(repo, tmp_path, backend: str = "stub", **kw) -> ConversationManager:
    settings = make_settings(mcp=GATEWAY_MCP)
    for key, value in kw.items():
        setattr(settings.core, key, value)
    return ConversationManager(
        repo=repo,
        settings=settings,
        composer=FakeComposer(),
        derive_profile=fake_derive_profile,
        gateway_url="http://gw:8000",
        gateway_token="tok",
        agent_backend=backend,
        data_dir=tmp_path,
    )


async def test_the_sdk_session_reports_a_failed_gateway_server(monkeypatch, tmp_path):
    install_fake_sdk(monkeypatch)
    monkeypatch.setattr(agent, "ClaudeSDKClient", FailedHealthClient)
    mgr = make_manager(FakeRepo(), tmp_path, backend="sdk")
    channel, conversation = make_channel(), make_conversation()

    await mgr.run_turn(channel, conversation, "hi")

    mc = mgr._pool["c1"]
    assert isinstance(mc.session, AgentSession)
    assert await mc.session.failed_mcp_servers() == ["health"]
    assert mc.replace_pending is True


async def test_a_connected_server_does_not_mark_the_session(monkeypatch, tmp_path):
    install_fake_sdk(monkeypatch)
    mgr = make_manager(FakeRepo(), tmp_path, backend="sdk")
    await mgr.run_turn(make_channel(), make_conversation(), "hi")
    assert mgr._pool["c1"].replace_pending is False


class SwitchClient(FakeSDKClient):
    """An SDK client whose ``health`` status a test sets per client."""

    instances: list[SwitchClient] = []

    def __init__(self, options):
        super().__init__(options)
        self.health = "connected"
        SwitchClient.instances.append(self)

    async def get_mcp_status(self):
        return {"mcpServers": [{"name": "health", "status": self.health}]}


async def test_the_next_turn_gets_a_new_session_once_in_the_window(monkeypatch, tmp_path):
    clock = [1000.0]
    monkeypatch.setattr(manager, "monotonic", lambda: clock[0])
    install_fake_sdk(monkeypatch)
    SwitchClient.instances = []
    monkeypatch.setattr(agent, "ClaudeSDKClient", SwitchClient)
    mgr = make_manager(FakeRepo(), tmp_path, backend="sdk")
    channel, conversation = make_channel(), make_conversation()

    await mgr.run_turn(channel, conversation, "first")
    mc = mgr._pool["c1"]
    first = mc.session
    SwitchClient.instances[-1].health = "failed"

    # The turn that sees the failure finishes on its own session.
    await mgr.run_turn(channel, conversation, "second")
    assert mc.session is first
    assert mc.replace_pending is True

    # The next turn runs on a new session, with a new client.
    await mgr.run_turn(channel, conversation, "third")
    second = mc.session
    assert second is not first
    assert len(SwitchClient.instances) == 2
    assert mc.replace_pending is False

    # The new client fails too, inside the window: the session is kept.
    SwitchClient.instances[-1].health = "failed"
    clock[0] += 60
    await mgr.run_turn(channel, conversation, "fourth")
    await mgr.run_turn(channel, conversation, "fifth")
    assert mc.session is second
    assert mc.replace_pending is False

    # After the window, one more replacement.
    clock[0] += 300
    await mgr.run_turn(channel, conversation, "sixth")
    assert mc.replace_pending is True
    await mgr.run_turn(channel, conversation, "seventh")
    assert mc.session is not second
    assert len(SwitchClient.instances) == 3


async def test_the_stub_reports_the_servers_a_test_sets(tmp_path):
    mgr = make_manager(FakeRepo(), tmp_path)
    channel, conversation = make_channel(), make_conversation()
    await mgr.run_turn(channel, conversation, "first")
    mc = mgr._pool["c1"]
    mc.session.failed_servers = ["health"]
    await mgr.run_turn(channel, conversation, "second")
    assert mc.replace_pending is True


async def test_a_window_of_zero_turns_the_replacement_off(tmp_path):
    mgr = make_manager(FakeRepo(), tmp_path, mcp_replace_seconds=0)
    channel, conversation = make_channel(), make_conversation()
    await mgr.run_turn(channel, conversation, "first")
    mc = mgr._pool["c1"]
    mc.session.failed_servers = ["health"]
    await mgr.run_turn(channel, conversation, "second")
    assert mc.replace_pending is False


async def test_a_status_that_cannot_be_read_marks_nothing(monkeypatch, tmp_path):
    class BrokenStatusClient(FakeSDKClient):
        async def get_mcp_status(self):
            raise RuntimeError("no status")

    install_fake_sdk(monkeypatch)
    monkeypatch.setattr(agent, "ClaudeSDKClient", BrokenStatusClient)
    mgr = make_manager(FakeRepo(), tmp_path, backend="sdk")
    result = await mgr.run_turn(make_channel(), make_conversation(), "hi")
    assert result.text == "Hello world"
    assert mgr._pool["c1"].replace_pending is False


def test_the_default_window_is_five_minutes():
    assert make_settings().core.mcp_replace_seconds == 300
