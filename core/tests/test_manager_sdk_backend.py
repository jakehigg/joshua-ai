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

    session, deps, profile, _paths, _mtime = await manager._build_session(
        channel, conversation, resume=None
    )
    assert profile == "default"

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
