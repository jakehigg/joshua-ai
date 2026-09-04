"""Stub-backend turn tests: transcript rows, session reuse, resume retry, and the
echo-tools probe."""

from __future__ import annotations

from datetime import date

from engine_fakes import (
    FakeComposer,
    FakeRepo,
    fake_derive_profile,
    make_channel,
    make_conversation,
    make_settings,
)
from joshua_core.engine.manager import ConversationManager
from joshua_core.engine.types import Attachment
from joshua_shared import config as config_module
from joshua_shared import layout

FILES_MCP = """
mcp:
  files:
    kind: builtin
    allow: all
"""

# A roster where Alex journals automatically and Sam has it turned off.
JOURNAL_CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    journal: auto
  - id: sam
    name: Sam
    journal: off
"""


def make_manager(repo: FakeRepo, tmp_path, **settings_kw) -> ConversationManager:
    return ConversationManager(
        repo=repo,
        settings=make_settings(**settings_kw),
        composer=FakeComposer(),
        derive_profile=fake_derive_profile,
        agent_backend="stub",
        data_dir=tmp_path,
    )


def make_journal_manager(repo: FakeRepo, tmp_path) -> ConversationManager:
    return ConversationManager(
        repo=repo,
        settings=config_module.parse(JOURNAL_CONFIG, env={}, source="<test>"),
        composer=FakeComposer(),
        derive_profile=fake_derive_profile,
        agent_backend="stub",
        data_dir=tmp_path,
    )


async def test_turn_writes_in_and_out_rows_with_turn_id(tmp_path):
    repo = FakeRepo()
    manager = make_manager(repo, tmp_path)
    channel, conversation = make_channel(), make_conversation()

    result = await manager.run_turn(channel, conversation, "hello there")

    assert result.text.startswith("(stub)")
    directions = [row["direction"] for row in repo.transcripts]
    assert directions == ["in", "out"]
    in_row, out_row = repo.transcripts
    assert in_row["content"] == "hello there"
    assert in_row["meta"]["turn_id"]
    assert out_row["meta"]["turn_id"] == in_row["meta"]["turn_id"]
    assert out_row["status"] == "ok"
    # The scratch cwd is created under the data dir.
    assert (tmp_path / "inbox" / "sessions" / "c1").is_dir()


async def test_second_turn_reuses_pooled_session(tmp_path):
    repo = FakeRepo()
    manager = make_manager(repo, tmp_path)
    channel, conversation = make_channel(), make_conversation()

    await manager.run_turn(channel, conversation, "first")
    session_after_first = manager._pool["c1"].session
    await manager.run_turn(channel, conversation, "second")

    assert len(manager._pool) == 1
    assert manager._pool["c1"].session is session_after_first


async def test_reaper_closes_idle_session(tmp_path):
    repo = FakeRepo()
    manager = make_manager(repo, tmp_path, idle_seconds=1)
    channel, conversation = make_channel(), make_conversation()

    await manager.run_turn(channel, conversation, "hi")
    assert len(manager._pool) == 1
    # Age the session past its TTL, then reap.
    manager._pool["c1"].last_used -= 100
    reaped = await manager._reap_idle()

    assert reaped == 1
    assert manager._pool == {}


async def test_reaper_disabled_when_ttl_not_positive(tmp_path):
    repo = FakeRepo()
    manager = make_manager(repo, tmp_path, idle_seconds=0)
    channel, conversation = make_channel(), make_conversation()
    await manager.run_turn(channel, conversation, "hi")
    manager._pool["c1"].last_used -= 100
    assert await manager._reap_idle() == 0
    assert len(manager._pool) == 1


async def test_resume_retry_rebuilds_without_resume(tmp_path):
    repo = FakeRepo()
    manager = make_manager(repo, tmp_path)
    channel = make_channel()
    # A bogus resume id makes the stub fail the first run; the manager rebuilds.
    conversation = make_conversation(sdk_session_id="bogus-session")

    await manager._get_or_create(channel, conversation)
    first_session = manager._pool["c1"].session

    result = await manager.run_turn(channel, conversation, "hello")

    assert result.text.startswith("(stub)")
    assert manager._pool["c1"].session is not first_session
    assert repo.sdk_sessions["c1"] == "stub-c1"
    out_row = repo.transcripts[-1]
    assert out_row["direction"] == "out"
    assert out_row["status"] == "ok"


async def test_echo_tools_lists_configured_servers(tmp_path):
    repo = FakeRepo()
    manager = make_manager(repo, tmp_path, mcp=FILES_MCP)
    manager.register_builtin("scheduling", lambda deps: object())
    channel, conversation = make_channel(), make_conversation()

    result = await manager.run_turn(channel, conversation, "[[echo-tools]]")

    assert result.text == "tools: files, scheduling"


async def test_journal_marker_writes_post_for_the_speaker(tmp_path):
    repo = FakeRepo()
    manager = make_journal_manager(repo, tmp_path)
    channel, conversation = make_channel(), make_conversation(person_id="alex")
    att = Attachment(
        path="attachments/2026/08/2026-08-27-090000-IMG.jpg",
        mime="image/jpeg",
        name="2026-08-27-090000-IMG.jpg",
        original_name="garden.jpg",
    )

    await manager.run_turn(
        channel, conversation, "I started a new fertilizer today [[journal]]", attachments=[att]
    )

    entries = list(layout.journal_day_dir(date.today(), tmp_path).glob("*-journal.md"))
    assert len(entries) == 1
    body = entries[0].read_text()
    assert "people: [alex]" in body
    assert "attachments/2026/08/2026-08-27-090000-IMG.jpg" in body
    assert manager._pool["c1"].session.journal_writes == 1


async def test_journal_off_writes_no_post(tmp_path):
    repo = FakeRepo()
    manager = make_journal_manager(repo, tmp_path)
    channel = make_channel()
    conversation = make_conversation(person_id="sam")

    await manager.run_turn(channel, conversation, "I painted the fence [[journal]]")

    assert manager._pool["c1"].session.journal_writes == 0
    assert not list(layout.journal_root(tmp_path).rglob("*-journal.md"))


async def test_group_journal_goes_to_the_resolved_speaker(tmp_path):
    repo = FakeRepo()
    manager = make_journal_manager(repo, tmp_path)
    channel = make_channel(session_mode="shared")
    conversation = make_conversation(person_id=None)

    await manager.run_turn(
        channel,
        conversation,
        "I ran my first 10k [[journal]]",
        speaker="Alex",
        person_id="alex",
    )

    entries = list(layout.journal_day_dir(date.today(), tmp_path).glob("*-journal.md"))
    assert len(entries) == 1
    assert "people: [alex]" in entries[0].read_text()
    assert not (tmp_path / "shared").exists()


async def test_group_journal_without_speaker_writes_nothing(tmp_path):
    repo = FakeRepo()
    manager = make_journal_manager(repo, tmp_path)
    channel = make_channel(session_mode="shared")
    conversation = make_conversation(person_id=None)

    await manager.run_turn(channel, conversation, "nice day [[journal]]")

    assert manager._pool["c1"].session.journal_writes == 0


async def test_shutdown_and_flush_clear_pool(tmp_path):
    repo = FakeRepo()
    manager = make_manager(repo, tmp_path)
    channel, conversation = make_channel(), make_conversation()
    await manager.run_turn(channel, conversation, "hi")

    snapshot = await manager.pool_snapshot()
    assert snapshot["count"] == 1
    await manager.interrupt("c1")

    flushed = await manager.flush_sessions()
    assert flushed == 1
    assert manager._pool == {}

    await manager.run_turn(channel, conversation, "again")
    await manager.shutdown()
    assert manager._pool == {}
