"""The voice profile: what a spoken turn adds, and the shorter session life.

A spoken turn takes the profile it would take anyway and adds the voice files to
it. Nothing is substituted, so a guest on a call keeps the guest prompt. These
tests run offline against the packaged prompts and the stub backend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from engine_fakes import FakeComposer, FakeRepo, make_conversation, make_settings
from joshua_core.engine.manager import ConversationManager
from joshua_core.engine.profiles import VOICE_FILES, derive_profile
from joshua_core.engine.prompts import PromptComposer
from joshua_core.store.models import Channel, Person
from joshua_shared import config as config_module

_MEMBER = Person(id="alex", display_name="Alex", role="member")
_GUEST = Person(id="gwen", display_name="Gwen", role="guest")

_VOICE = Channel(id="voice:threads", channel_type="voice", session_mode="per_person")
_TELEGRAM = Channel(id="telegram:1", channel_type="telegram", session_mode="per_person")
_VOICE_GROUP = Channel(id="voice:kitchen", channel_type="voice", session_mode="shared")

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
    max_turns: "8"
    idle_ttl_s: "300"
"""


def _voice_config() -> Any:
    return config_module.parse(VOICE_CONFIG, env={}, source="<test>").channels.voice


# --- what a spoken turn adds ------------------------------------------------


def test_a_call_adds_the_voice_files_to_the_ordinary_profile() -> None:
    typed = derive_profile(_MEMBER, _TELEGRAM)
    spoken = derive_profile(_MEMBER, _VOICE, voice=_voice_config())

    assert spoken.prompt_files == (*typed.prompt_files, *VOICE_FILES)
    assert spoken.name == "voice-dm"


def test_a_guest_on_a_call_keeps_the_guest_prompt() -> None:
    spoken = derive_profile(_GUEST, _VOICE, voice=_voice_config())
    assert "guest.md" in spoken.prompt_files
    assert spoken.prompt_files[-2:] == VOICE_FILES
    assert spoken.name == "voice-guest"


def test_a_group_on_a_call_keeps_the_group_prompt() -> None:
    spoken = derive_profile(None, _VOICE_GROUP, voice=_voice_config())
    assert "group.md" in spoken.prompt_files
    assert spoken.prompt_files[-2:] == VOICE_FILES
    assert spoken.name == "voice-group"


def test_the_voice_files_carry_the_configured_ceilings() -> None:
    spoken = derive_profile(_MEMBER, _VOICE, voice=_voice_config())
    assert spoken.model == "claude-sonnet-5"
    assert spoken.max_turns == 8
    assert spoken.idle_ttl_s == 300


def test_a_text_channel_never_loads_a_voice_file() -> None:
    typed = derive_profile(_MEMBER, _TELEGRAM, voice=_voice_config())
    assert typed.name == "dm"
    assert not set(VOICE_FILES) & set(typed.prompt_files)
    assert typed.model is None
    assert typed.idle_ttl_s is None


def test_without_the_voice_section_a_call_is_an_ordinary_dm() -> None:
    """The channel type alone must not change a session. Config turns it on."""
    profile = derive_profile(_MEMBER, _VOICE, voice=None)
    assert profile.name == "dm"
    assert not set(VOICE_FILES) & set(profile.prompt_files)


# --- what the prompt says ---------------------------------------------------


def test_voice_needs_no_prompt_of_your_own() -> None:
    """A deployment that turns the channel on writes no prompt file.

    Everything a spoken turn needs ships in the image: the call markers, the
    brevity rule, and how to write words a speech engine reads correctly.
    """
    spoken = PromptComposer().compose(
        derive_profile(_MEMBER, _VOICE, voice=_voice_config()), _MEMBER, _VOICE
    )
    assert "[DONE]" in spoken
    assert "[IGNORE]" in spoken
    assert "one or two sentences" in spoken.lower()
    assert "degrees Fahrenheit" in spoken


def test_the_voice_guidance_comes_after_the_ordinary_prompt() -> None:
    """The two can disagree about formatting, so the spoken rule must land last."""
    spoken = PromptComposer().compose(
        derive_profile(_MEMBER, _VOICE, voice=_voice_config()), _MEMBER, _VOICE
    )
    assert spoken.index("You are replying in a chat") < spoken.index("You are talking out loud")


def test_a_typed_turn_never_sees_the_voice_guidance() -> None:
    typed = PromptComposer().compose(derive_profile(_MEMBER, _TELEGRAM), _MEMBER, _TELEGRAM)
    assert "[IGNORE]" not in typed
    assert "You are talking out loud" not in typed


# --- when a voice session closes --------------------------------------------


def _profile_for(name: str, idle_ttl_s: int | None) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(name=name, model=None, max_turns=0, idle_ttl_s=idle_ttl_s)


def _manager(repo: FakeRepo, tmp_path: Path, ttl: int | None, **settings_kw: Any):
    return ConversationManager(
        repo=repo,
        settings=make_settings(**settings_kw),
        composer=FakeComposer(),
        derive_profile=lambda person, channel: _profile_for("voice-dm", ttl),
        agent_backend="stub",
        data_dir=tmp_path,
    )


@pytest.mark.asyncio
async def test_a_voice_session_closes_on_its_own_deadline(tmp_path: Path) -> None:
    repo = FakeRepo()
    manager = _manager(repo, tmp_path, 300, idle_seconds=1800)
    managed = await manager._get_or_create(_TELEGRAM, make_conversation())
    assert managed.idle_ttl_s == 300

    # Idle for six minutes: past the voice deadline, far inside the default one.
    managed.last_used -= 360
    assert await manager._reap_idle() == 1
    assert manager._pool == {}


@pytest.mark.asyncio
async def test_a_session_with_no_deadline_of_its_own_uses_the_default(tmp_path: Path) -> None:
    repo = FakeRepo()
    manager = _manager(repo, tmp_path, None, idle_seconds=1800)
    managed = await manager._get_or_create(_TELEGRAM, make_conversation())
    managed.last_used -= 360
    assert await manager._reap_idle() == 0

    managed.last_used -= 1800
    assert await manager._reap_idle() == 1


@pytest.mark.asyncio
async def test_a_deadline_of_zero_keeps_the_session(tmp_path: Path) -> None:
    repo = FakeRepo()
    manager = _manager(repo, tmp_path, 0, idle_seconds=60)
    managed = await manager._get_or_create(_TELEGRAM, make_conversation())
    managed.last_used -= 100_000
    assert await manager._reap_idle() == 0
