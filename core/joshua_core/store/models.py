"""Frozen dataclasses that mirror the store rows.

Stdlib only, so they import without psycopg. Every field maps to a column;
``workspace_dir`` and ``prompt_profile`` are gone in v4 (a person's workspace is
``/data/people/<id>``, and the prompt profile is derived from role and chat
kind).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class Channel:
    """An addressable endpoint core can hear from or speak to.

    ``id`` is namespaced, e.g. ``telegram:123456789``.
    """

    id: str
    channel_type: str
    display_name: str | None = None
    default_person_id: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    # 'per_person' (DMs) or 'shared' (group chats, speaker-labelled).
    session_mode: str = "per_person"
    created_at: datetime | None = None


@dataclass(frozen=True)
class Person:
    """A member or guest. The roster mirrors ``people``."""

    id: str
    display_name: str
    role: str = "member"  # member | guest
    created_at: datetime | None = None


@dataclass(frozen=True)
class Conversation:
    """One (channel, person) thread. Carries the SDK session for resume.

    ``person_id`` is None for unidentified turns.
    """

    id: str
    channel_id: str
    person_id: str | None
    sdk_session_id: str | None = None
    last_active_at: datetime | None = None
    # Most recent physical channel a turn ran on; the scheduler delivers here.
    last_channel_id: str | None = None
    # Date the current SDK session started; the phase 4 daily rollover reads it.
    session_started_on: date | None = None


@dataclass(frozen=True)
class Task:
    """An agent-schedulable task. One live row per task."""

    id: str
    conversation_id: str
    prompt: str
    next_run_at: datetime
    cron_expr: str | None = None
    status: str = "active"  # active | paused | completed | failed | processing
    last_run_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime | None = None
