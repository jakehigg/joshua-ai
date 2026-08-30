"""Derived session profiles.

A profile decides which kernel prompt files compose a session's system prompt and
carries the per-session engine ceilings (model, max turns, idle TTL). v4 derives
the profile from the person's role and the chat kind — there is no
``profiles.yaml`` matrix. Tool exposure comes from the gateway config, not from
the profile.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from joshua_core.store.models import Channel, Person

# name -> the ordered prompt files that compose that profile's system prompt.
_PROMPT_FILES: dict[str, tuple[str, ...]] = {
    "dm": ("base.md", "people.md", "chat.md", "registration.md"),
    "group": ("base.md", "people.md", "chat.md", "group.md", "registration.md"),
    "guest": ("base.md", "guest.md", "chat.md"),
    "event": ("base.md", "people.md", "event.md"),
    "scheduled": ("base.md", "people.md", "scheduled.md"),
}


@dataclass(frozen=True)
class Profile:
    name: str  # "dm" | "group" | "guest" | "event" | "scheduled"
    prompt_files: tuple[str, ...]
    model: str | None = None  # None -> core.model
    max_turns: int | None = None
    idle_ttl_s: int | None = None

    def with_files(self, extra: tuple[str, ...]) -> Profile:
        """Append module prompt files (phase 6 registry) to a copy of this profile."""
        return replace(self, prompt_files=(*self.prompt_files, *extra))


def derive_profile(person: Person | None, channel: Channel, *, kind: str = "message") -> Profile:
    """Pick the profile for a turn from the chat kind, the channel mode, and the
    person's role."""
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
    return Profile(name=name, prompt_files=_PROMPT_FILES[name])
