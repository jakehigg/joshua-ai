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
