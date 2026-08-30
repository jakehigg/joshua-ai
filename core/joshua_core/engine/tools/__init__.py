"""In-process SDK MCP tool servers (scheduling, registration).

Each server is built per conversation as a closure over ``ToolDeps`` so a tool
call always acts on the conversation that invoked it. The scheduling and people
tickets own the server builders; this package owns the shared dependency record.
"""

from __future__ import annotations

from dataclasses import dataclass

from joshua_core.store.models import Channel, Conversation
from joshua_core.store.repo import Repo


@dataclass
class ToolDeps:
    repo: Repo
    conversation: Conversation
    channel: Channel
    tz: str
    # The current turn's speaker, rebound per turn by the manager under the
    # session lock. For a per-person session this equals ``conversation.person_id``;
    # for a shared session it is the resolved sender (or None). Member-gated tools
    # read this, not the session's fixed ``conversation.person_id``.
    person_id: str | None = None
