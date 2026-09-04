"""Pool cap and LRU eviction, ported from the warm-pool cases that cover them."""

from __future__ import annotations

from engine_fakes import (
    FakeComposer,
    FakeRepo,
    fake_derive_profile,
    make_channel,
    make_conversation,
    make_settings,
)
from joshua_core.engine import manager
from joshua_core.engine.manager import ConversationManager


def make_manager(repo: FakeRepo, tmp_path, **settings_kw) -> ConversationManager:
    return ConversationManager(
        repo=repo,
        settings=make_settings(**settings_kw),
        composer=FakeComposer(),
        derive_profile=fake_derive_profile,
        agent_backend="stub",
        data_dir=tmp_path,
    )


async def test_pool_cap_evicts_least_recently_used(tmp_path):
    repo = FakeRepo()
    manager = make_manager(repo, tmp_path, pool_max=2)
    channel = make_channel()

    for i in range(3):
        conv = make_conversation(conversation_id=f"c{i}", person_id="alex")
        await manager._get_or_create(channel, conv)

    # The oldest (c0) is evicted; the pool holds the two most recent.
    assert set(manager._pool) == {"c1", "c2"}


async def test_pool_cap_keeps_pool_within_limit(tmp_path):
    repo = FakeRepo()
    manager = make_manager(repo, tmp_path, pool_max=3)
    channel = make_channel()

    for i in range(6):
        conv = make_conversation(conversation_id=f"c{i}", person_id="alex")
        await manager._get_or_create(channel, conv)

    assert len(manager._pool) == 3


async def test_get_or_create_returns_same_managed(tmp_path):
    repo = FakeRepo()
    manager = make_manager(repo, tmp_path)
    channel, conversation = make_channel(), make_conversation()

    first = await manager._get_or_create(channel, conversation)
    second = await manager._get_or_create(channel, conversation)
    assert first is second


# -- What a turn actually wrote ------------------------------------------------
#
# `written` names the writes that landed. A refused write is not there, so an
# operator reading the line does not look for a file that was never made.


def _call(name: str, path: str, ok: bool) -> dict:
    return {"id": f"t-{path}", "name": name, "input": {"path": path}, "ok": ok}


def test_a_refused_write_is_not_reported_as_written() -> None:
    calls = [_call("mcp__files__write_file", "wiki/nope.md", ok=False)]
    assert manager.written_paths(calls) == []
    assert manager.failed_writes(calls) == 1


def test_a_mixed_turn_reports_the_write_that_landed() -> None:
    calls = [
        _call("mcp__files__write_file", "wiki/notes/ok.md", ok=True),
        _call("mcp__files__write_file", "journal/retired.md", ok=False),
    ]
    assert manager.written_paths(calls) == ["wiki/notes/ok.md"]
    assert manager.failed_writes(calls) == 1


def test_a_turn_with_no_write_call_is_unchanged() -> None:
    calls = [
        {"id": "t-1", "name": "mcp__files__read_file", "input": {"path": "wiki/x.md"}, "ok": True}
    ]
    assert manager.written_paths(calls) == []
    assert manager.failed_writes(calls) == 0


def test_a_call_with_no_outcome_counts_as_failed() -> None:
    """Silence is not success. A missing result must not read as a write."""
    calls = [{"id": "t-1", "name": "mcp__files__write_file", "input": {"path": "wiki/x.md"}}]
    assert manager.written_paths(calls) == []
    assert manager.failed_writes(calls) == 1
