"""SDK-backend session build and turn, driven by a fake SDK client.

Covers the branch that wires gateway servers plus the in-process builtins and
returns a live ``ToolDeps``.
"""

from __future__ import annotations

from engine_fakes import (
    FakeComposer,
    FakeRepo,
    fake_derive_profile,
    install_fake_sdk,
    make_channel,
    make_conversation,
    make_settings,
)
from joshua_core.engine.manager import ConversationManager

FILES_MCP = """
mcp:
  files:
    kind: builtin
    allow: all
"""


def make_manager(repo, tmp_path, builtins=None) -> ConversationManager:
    return ConversationManager(
        repo=repo,
        settings=make_settings(mcp=FILES_MCP),
        composer=FakeComposer(),
        derive_profile=fake_derive_profile,
        gateway_url="http://gw:8000",
        gateway_token="tok",
        agent_backend="sdk",
        data_dir=tmp_path,
        builtin_factories=builtins or {},
    )


async def test_build_session_wires_gateway_and_builtins(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    repo = FakeRepo()
    captured: dict = {}

    def scheduling(deps):
        captured["deps"] = deps
        return {"kind": "sdk-server"}

    manager = make_manager(repo, tmp_path, builtins={"scheduling": scheduling})
    channel, conversation = make_channel(), make_conversation()

    built = await manager._build_session(channel, conversation, resume=None)
    session, deps, profile = built.session, built.deps, built.profile
    assert profile.name == "default"

    options = session._options
    assert set(options.mcp_servers) == {"files", "scheduling"}
    assert options.mcp_servers["files"]["headers"]["X-Joshua-Person"] == "alex"
    assert options.mcp_servers["files"]["headers"]["X-Joshua-Conversation"] == "c1"
    assert captured["deps"] is deps
    assert deps.person_id == "alex"
    assert deps.tz == "America/New_York"


async def test_run_turn_sdk_backend_persists_session(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    repo = FakeRepo()
    manager = make_manager(repo, tmp_path)
    channel, conversation = make_channel(), make_conversation()

    result = await manager.run_turn(channel, conversation, "hi")

    assert result.text == "Hello world"
    assert repo.sdk_sessions["c1"] == "sess-9"
    assert manager._pool["c1"].deps.channel is channel
    assert [row["direction"] for row in repo.transcripts] == ["in", "out"]


GROUP_MCP = """
groups:
  - id: team
    channel: telegram
    chat_id: "-100200"
    members:
      - "101"
mcp:
  files:
    kind: builtin
    allow: all
  tracker:
    type: http
    url: http://upstream/tracker
    allow:
      - team
"""


def make_group_manager(repo, tmp_path) -> ConversationManager:
    return ConversationManager(
        repo=repo,
        settings=make_settings(mcp=GROUP_MCP),
        composer=FakeComposer(),
        derive_profile=fake_derive_profile,
        gateway_url="http://gw:8000",
        gateway_token="tok",
        agent_backend="sdk",
        data_dir=tmp_path,
        builtin_factories={},
    )


async def test_a_group_chat_reaches_the_server_that_allows_its_group(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    manager = make_group_manager(FakeRepo(), tmp_path)
    channel = make_channel("telegram:-100200", session_mode="shared")
    conversation = make_conversation(person_id=None, channel_id="telegram:-100200")

    built = await manager._build_session(channel, conversation, resume=None)

    servers = built.session._options.mcp_servers
    assert set(servers) == {"files", "tracker"}
    assert servers["tracker"]["headers"]["X-Joshua-Group"] == "team"
    assert servers["tracker"]["headers"]["X-Joshua-Person"] == "unknown"


async def test_another_group_chat_does_not_reach_that_server(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    manager = make_group_manager(FakeRepo(), tmp_path)
    channel = make_channel("telegram:-100999", session_mode="shared")
    conversation = make_conversation(person_id=None, channel_id="telegram:-100999")

    built = await manager._build_session(channel, conversation, resume=None)

    servers = built.session._options.mcp_servers
    assert set(servers) == {"files"}
    assert "X-Joshua-Group" not in servers["files"]["headers"]


async def test_a_direct_message_sends_no_group(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    manager = make_group_manager(FakeRepo(), tmp_path)

    built = await manager._build_session(make_channel(), make_conversation(), resume=None)

    servers = built.session._options.mcp_servers
    assert set(servers) == {"files"}
    assert "X-Joshua-Group" not in servers["files"]["headers"]
