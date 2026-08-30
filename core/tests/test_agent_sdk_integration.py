"""SDK backend smoke test: a turn through ``ConversationManager`` invokes the real
Claude Agent SDK.

Marked ``integration``. It spawns the bundled ``claude`` CLI and calls the model,
so it is skipped unless ``CLAUDE_CODE_OAUTH_TOKEN`` is set (CI runs without it).
The composer and profile deriver are test doubles, so the turn exercises only the
real ``AgentSession``.
"""

from __future__ import annotations

import os

import pytest
from engine_fakes import (
    FakeComposer,
    FakeRepo,
    fake_derive_profile,
    make_channel,
    make_conversation,
    make_settings,
)
from joshua_core.engine.manager import ConversationManager

pytestmark = pytest.mark.integration

_HAS_TOKEN = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))


@pytest.mark.skipif(not _HAS_TOKEN, reason="needs CLAUDE_CODE_OAUTH_TOKEN and the claude CLI")
async def test_sdk_backend_runs_a_real_turn(tmp_path) -> None:
    manager = ConversationManager(
        repo=FakeRepo(),
        settings=make_settings(),
        composer=FakeComposer(),
        derive_profile=fake_derive_profile,
        agent_backend="sdk",
        data_dir=tmp_path,
    )
    channel, conversation = make_channel(), make_conversation()
    try:
        result = await manager.run_turn(channel, conversation, "Reply with the single word: pong.")
    finally:
        await manager.shutdown()

    assert not result.is_error
    assert result.text
    assert result.session_id
