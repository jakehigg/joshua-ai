"""The one place the path filter runs: the reply of a turn.

The filter runs where the reply is stored and delivered, so the transcript row
of the turn says what every text channel gets. A spoken turn is not touched,
and ``core.path_links: false`` turns the filter off.
"""

from __future__ import annotations

from typing import Any

from engine_fakes import FakeComposer, FakeRepo, fake_derive_profile, make_channel, make_settings
from joshua_core.engine.manager import ConversationManager
from joshua_core.store.models import Channel, Conversation
from joshua_shared import config as config_module

VOICE_CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    handles:
      voice: alex
channels:
  voice:
    model: claude-sonnet-5
"""


def _manager(repo: FakeRepo, tmp_path, settings: Any) -> ConversationManager:
    return ConversationManager(
        repo=repo,
        settings=settings,
        composer=FakeComposer(),
        derive_profile=fake_derive_profile,
        agent_backend="stub",
        data_dir=tmp_path,
    )


def _conversation(channel_id: str) -> Conversation:
    return Conversation(id="c1", person_id="alex", channel_id=channel_id)


async def test_a_text_turn_stores_the_reply_with_the_path_linked(tmp_path):
    repo = FakeRepo()
    manager = _manager(repo, tmp_path, make_settings())

    result = await manager.run_turn(
        make_channel(), _conversation("telegram:1"), "where is wiki/recipes/pie.md"
    )

    assert "[wiki/recipes/pie.md](/wiki/recipes/pie.md)" in result.text
    out_row = repo.transcripts[-1]
    assert "[wiki/recipes/pie.md](/wiki/recipes/pie.md)" in out_row["content"]


async def test_a_voice_turn_sends_the_text_with_no_change(tmp_path):
    repo = FakeRepo()
    settings = config_module.parse(VOICE_CONFIG, env={}, source="<test>")
    manager = _manager(repo, tmp_path, settings)
    channel = Channel(id="voice:threads", channel_type="voice", session_mode="per_person")

    result = await manager.run_turn(
        channel, _conversation("voice:threads"), "where is wiki/recipes/pie.md"
    )

    assert "wiki/recipes/pie.md" in result.text
    assert "[wiki/recipes/pie.md]" not in result.text


async def test_one_setting_turns_the_filter_off(tmp_path):
    repo = FakeRepo()
    settings = config_module.parse(
        "name: Test House\ntimezone: America/New_York\n"
        "people:\n  - id: alex\n    name: Alex\n"
        "core:\n  path_links: false\n",
        env={},
        source="<test>",
    )
    manager = _manager(repo, tmp_path, settings)

    result = await manager.run_turn(
        make_channel(), _conversation("telegram:1"), "where is wiki/recipes/pie.md"
    )

    assert "wiki/recipes/pie.md" in result.text
    assert "[wiki/recipes/pie.md]" not in result.text
