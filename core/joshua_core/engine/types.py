"""SDK-free engine types.

Kept apart from ``agent.py`` so the manager, the stub backend, and the tests
import ``TurnResult`` without importing ``claude_agent_sdk`` (which is absent in
stub mode and in the offline test run).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

OnDelta = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class Attachment:
    """One inbound file the agent may view through the files MCP server.

    ``path`` is files-MCP-relative, e.g. ``attachments/2026/08/x.jpg``.
    ``original_name`` is what the sender called the file, so the agent can name it
    back to the person and cite it in a journal post.
    """

    path: str
    mime: str
    name: str
    original_name: str | None = None


@dataclass
class TurnResult:
    text: str
    session_id: str | None = None
    is_error: bool = False
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    # De-duped names of the tools the agent invoked this turn, first-seen order.
    tools_used: list[str] = field(default_factory=list)
    # Per-call record ``[{"name": ..., "input": {...}}, ...]`` in call order, with
    # duplicates. ``tools_used`` is the de-duped name list derived from it.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
