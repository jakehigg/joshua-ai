"""The main thread: a viewer turn runs in the person's main conversation, never
moves its last channel, and the turn names its own channel."""

from __future__ import annotations

from typing import Any

from engine_fakes import (
    FakeComposer,
    FakeRepo,
    fake_derive_profile,
    make_channel,
    make_conversation,
)
from joshua_core.engine.manager import ConversationManager
from joshua_core.store.models import Channel, Conversation
from test_turns_api import (
    CHANNELS_TOKEN,
    TurnsFakeRepo,
    _fleet_tokens,  # noqa: F401 — the autouse fixture of the fleet tokens
    bearer,
    build_turns,
    core_client,
    dm_event,
    make_settings,
)


class MainRepo(TurnsFakeRepo):
    """A fake repo with an iMessage DM that is the main conversation of alex."""

    def __init__(self) -> None:
        super().__init__()
        self.channels["imessage:+1"] = Channel(
            id="imessage:+1", channel_type="imessage", session_mode="per_person"
        )
        self.dm = Conversation(
            id="dm-1", channel_id="imessage:+1", person_id="alex", last_channel_id="imessage:+1"
        )
        self.conversations[("imessage:+1", "alex")] = self.dm
        self.last_channels: list[str | None] = []

    async def main_conversation(self, person_id: str) -> Conversation | None:
        return self.dm if person_id == "alex" else None

    async def set_sdk_session(
        self,
        conversation_id: str,
        session_id: str,
        last_channel_id: str | None = None,
        inference_endpoint: str | None = None,
    ) -> None:
        self.last_channels.append(last_channel_id)
        await super().set_sdk_session(conversation_id, session_id, last_channel_id)

    async def touch_conversation(
        self, conversation_id: str, last_channel_id: str | None = None
    ) -> None:
        self.last_channels.append(last_channel_id)
        await super().touch_conversation(conversation_id, last_channel_id)


def _cli(**extra: Any) -> dict[str, Any]:
    return dm_event(
        channel="cli:alex",
        chat={"kind": "dm", "id": "alex"},
        handle={"type": "cli", "id": "alex"},
        message_id=None,
        **extra,
    )


async def _stream(client, event) -> str:
    async with client.stream(
        "POST", "/v1/turns/stream", json=event, headers=bearer(CHANNELS_TOKEN)
    ) as resp:
        assert resp.status_code == 200
        return "".join([c async for c in resp.aiter_text()])


async def test_a_main_thread_turn_runs_in_the_dm_and_keeps_the_last_channel(tmp_path) -> None:
    repo = MainRepo()
    turns = build_turns(repo, make_settings(), [], tmp_path)
    async with core_client(turns) as client:
        body = await _stream(client, _cli(thread="main"))
    assert "event: done" in body
    assert {r["conversation_id"] for r in repo.transcripts} == {"dm-1"}
    assert repo.last_channels == [None]  # pull-only: last_channel_id is not set
    assert ("cli:alex", "alex") not in repo.conversations


async def test_without_the_thread_field_the_terminal_conversation_runs(tmp_path) -> None:
    repo = MainRepo()
    turns = build_turns(repo, make_settings(), [], tmp_path)
    async with core_client(turns) as client:
        await _stream(client, _cli())
    assert ("cli:alex", "alex") in repo.conversations
    assert "dm-1" not in {r["conversation_id"] for r in repo.transcripts}


async def test_with_no_main_conversation_the_terminal_one_is_the_fallback(tmp_path) -> None:
    repo = MainRepo()
    repo.dm = None  # type: ignore[assignment]

    async def none(person_id: str) -> None:
        return None

    repo.main_conversation = none  # type: ignore[method-assign]
    turns = build_turns(repo, make_settings(), [], tmp_path)
    async with core_client(turns) as client:
        await _stream(client, _cli(thread="main"))
    assert ("cli:alex", "alex") in repo.conversations


# --- the channel note --------------------------------------------------------


def _manager(tmp_path) -> ConversationManager:
    return ConversationManager(
        repo=FakeRepo(),
        settings=make_settings(),
        composer=FakeComposer(),
        derive_profile=fake_derive_profile,
        agent_backend="stub",
        data_dir=tmp_path,
    )


async def test_each_turn_names_its_own_channel_on_a_shared_thread(tmp_path) -> None:
    manager = _manager(tmp_path)
    phone = make_channel("imessage:+1")
    viewer = make_channel("cli:alex")
    conv = make_conversation(channel_id="imessage:+1")

    await manager.run_turn(phone, conv, "one")  # builds the session: no note
    await manager.run_turn(viewer, conv, "two", pull_only=True)
    await manager.run_turn(phone, conv, "three")  # back to the phone: a note again
    await manager.run_turn(phone, conv, "four")  # same channel as before: none

    prompts = manager._pool[conv.id].session.prompts
    assert "arrives on channel" not in prompts[0]
    assert "arrives on channel `cli:alex`" in prompts[1]
    assert "arrives on channel `imessage:+1`" in prompts[2]
    assert "arrives on channel" not in prompts[3]
