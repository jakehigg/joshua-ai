"""Unit tests for ``derive_profile`` and ``Profile``."""

from __future__ import annotations

from joshua_core.engine.profiles import Profile, derive_profile
from joshua_core.store.models import Channel, Person

_MEMBER = Person(id="alex", display_name="Alex", role="member")
_GUEST = Person(id="sam", display_name="Sam", role="guest")
_DM = Channel(id="telegram:1", channel_type="telegram", session_mode="per_person")
_SHARED = Channel(id="telegram:g", channel_type="telegram", session_mode="shared")


def test_derive_dm() -> None:
    profile = derive_profile(_MEMBER, _DM)
    assert profile.name == "dm"
    assert profile.prompt_files == ("base.md", "people.md", "chat.md", "registration.md")


def test_derive_group_from_shared_channel() -> None:
    profile = derive_profile(_MEMBER, _SHARED)
    assert profile.name == "group"
    assert profile.prompt_files == (
        "base.md",
        "people.md",
        "chat.md",
        "group.md",
        "registration.md",
    )


def test_derive_guest_from_role() -> None:
    profile = derive_profile(_GUEST, _DM)
    assert profile.name == "guest"
    assert profile.prompt_files == ("base.md", "guest.md", "chat.md")


def test_derive_event_from_kind() -> None:
    profile = derive_profile(None, _DM, kind="event")
    assert profile.name == "event"
    assert profile.prompt_files == ("base.md", "people.md", "event.md")


def test_derive_scheduled_from_task_kind() -> None:
    profile = derive_profile(_MEMBER, _DM, kind="task")
    assert profile.name == "scheduled"
    assert profile.prompt_files == ("base.md", "people.md", "scheduled.md")


def test_kind_beats_shared_and_role() -> None:
    # An event on a shared channel is still an event, not a group.
    assert derive_profile(_GUEST, _SHARED, kind="event").name == "event"


def test_dm_when_person_is_none() -> None:
    assert derive_profile(None, _DM).name == "dm"


def test_with_files_appends() -> None:
    profile = Profile(name="dm", prompt_files=("base.md",))
    extended = profile.with_files(("module.md",))
    assert extended.prompt_files == ("base.md", "module.md")
    # The original is unchanged (frozen).
    assert profile.prompt_files == ("base.md",)
