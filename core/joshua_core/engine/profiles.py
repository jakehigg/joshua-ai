"""Derived session profiles.

A profile decides which kernel prompt files compose a session's system prompt and
carries the per-session engine ceilings (model, max turns, idle TTL). The
profile comes from the person's role and the chat kind. There is no
``profiles.yaml`` matrix. Tool exposure comes from the gateway config, not from
the profile.

A spoken turn takes the profile it would take anyway, plus the voice files. It is
the same Joshua, on a channel that is heard instead of read.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from joshua_shared.config import VoiceChannel

from joshua_core.store.models import Channel, Person

# The channel type that a spoken turn arrives on.
VOICE_CHANNEL_TYPE = "voice"

# name -> the ordered prompt files that compose that profile's system prompt.
_PROMPT_FILES: dict[str, tuple[str, ...]] = {
    "dm": ("base.md", "people.md", "chat.md", "registration.md"),
    "group": ("base.md", "people.md", "chat.md", "group.md", "registration.md"),
    "guest": ("base.md", "guest.md", "chat.md"),
    "event": ("base.md", "people.md", "event.md"),
    "scheduled": ("base.md", "people.md", "scheduled.md"),
}

# What a spoken turn adds to whichever profile it took. ``voice.md`` is how to
# behave on a call and the two call markers; ``voice-speech.md`` is how to write
# words a speech engine reads correctly. They ship in the image, so a deployment
# that turns the voice channel on writes no prompt of its own. On any other
# channel they are never loaded.
VOICE_FILES = ("voice.md", "voice-speech.md")


@dataclass(frozen=True)
class Profile:
    name: str  # "dm" | "group" | "guest" | "event" | "scheduled", "voice-" prefixed on a call
    prompt_files: tuple[str, ...]
    model: str | None = None  # None -> core.model
    max_turns: int | None = None
    idle_ttl_s: int | None = None

    def with_files(self, extra: tuple[str, ...]) -> Profile:
        """Append module prompt files (phase 6 registry) to a copy of this profile."""
        return replace(self, prompt_files=(*self.prompt_files, *extra))


def derive_profile(
    person: Person | None,
    channel: Channel,
    *,
    kind: str = "message",
    voice: VoiceChannel | None = None,
) -> Profile:
    """Pick the profile for a turn from the chat kind, the channel, and the role."""
    if kind == "event":
        name = "event"
    elif kind == "task":
        name = "scheduled"
    elif channel.session_mode == "shared":
        name = "group"
    elif person is not None and person.role == "guest":
        name = "guest"
    else:
        name = "dm"
    profile = Profile(name=name, prompt_files=_PROMPT_FILES[name])
    if voice is not None and channel.channel_type == VOICE_CHANNEL_TYPE:
        return _spoken(profile, voice)
    return profile


def _spoken(profile: Profile, voice: VoiceChannel) -> Profile:
    """The same profile, with the voice files and the ceilings for a call.

    The voice files are appended, never substituted: a guest on a call keeps
    ``guest.md``, and a group keeps ``group.md``. The ceilings come from
    ``channels.voice``, because a spoken turn must answer fast, and a person who
    stops talking has left the room.
    """
    return replace(
        profile.with_files(VOICE_FILES),
        name=f"voice-{profile.name}",
        model=voice.model,
        max_turns=voice.max_turns,
        idle_ttl_s=voice.idle_ttl_s,
    )
