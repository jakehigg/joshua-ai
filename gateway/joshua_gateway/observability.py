"""Per-identity tool-call observability for the gateway.

The gateway proxies MCP tool calls for ``core`` and records each one, so an
operator can audit what the agent did on each person's behalf. This module holds two views:

1. **Call log** — a bounded in-memory ring of every proxied tool call, attributed
   to the authenticated caller identity, with server, tool, duration, status and
   error. The gateway runs one replica, so the ring is a complete recent view;
   the ring size comes from ``GATEWAY_CALL_LOG_SIZE`` (default 2000). Each call is
   also emitted as a structured log line by the upstream supervisor.

2. **Caller registry** — which identities the gateway has seen and which upstream
   servers each has touched, for the ``/admin/inventory`` endpoint.

The authenticated identity is put in ``identity_ctx`` by the ASGI route before the
request enters the session manager; the tool-call hook reads it back when the call
completes. Attribution is observational, never access control.
"""

from __future__ import annotations

import os
import threading
from collections import deque
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# Set by the ASGI route per request; read by the tool-call hook when it records a
# completed call. Defaults to "unknown" so a call outside any request context
# still logs cleanly.
identity_ctx: ContextVar[str] = ContextVar("gateway_identity", default="unknown")

# The person and conversation the ASGI route attributed this request to, read
# back by the tool-call hook. None when the caller asserted no person.
person_ctx: ContextVar[str | None] = ContextVar("gateway_person", default=None)
conversation_ctx: ContextVar[str | None] = ContextVar("gateway_conversation", default=None)

DEFAULT_CALL_LOG_SIZE = 2000


def call_log_size() -> int:
    """Return the ring size from ``GATEWAY_CALL_LOG_SIZE`` (default 2000)."""
    raw = os.environ.get("GATEWAY_CALL_LOG_SIZE", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_CALL_LOG_SIZE


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


class CallLog:
    """Bounded ring of recent proxied tool calls (newest evicts oldest)."""

    def __init__(self, maxlen: int):
        self._calls: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._calls.append(entry)

    def recent(
        self,
        identity: str | None = None,
        tool: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Recent calls, newest first, with optional identity/tool substring filters."""
        with self._lock:
            items = list(self._calls)
        if identity:
            needle = identity.strip().lower()
            items = [c for c in items if needle in str(c.get("identity", "")).lower()]
        if tool:
            needle = tool.strip().lower()
            items = [c for c in items if needle in str(c.get("tool", "")).lower()]
        items.reverse()
        if limit and limit > 0:
            items = items[:limit]
        return items


class CallerRegistry:
    """Which identities the gateway has seen, and which servers each has touched.

    "Touched" means any request routed to that server's endpoint, so an identity
    appears as soon as it connects, before its first tool call.
    """

    def __init__(self):
        self._callers: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _caller(self, identity: str, now: str) -> dict[str, Any]:
        entry = self._callers.get(identity)
        if entry is None:
            entry = {"first_seen": now, "last_seen": now, "calls": 0, "servers": {}}
            self._callers[identity] = entry
        return entry

    def touch(self, identity: str, server: str) -> None:
        now = now_iso()
        with self._lock:
            entry = self._caller(identity, now)
            entry["last_seen"] = now
            entry["servers"][server] = now

    def record_call(self, identity: str) -> None:
        now = now_iso()
        with self._lock:
            entry = self._caller(identity, now)
            entry["last_seen"] = now
            entry["calls"] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                identity: {
                    "first_seen": entry["first_seen"],
                    "last_seen": entry["last_seen"],
                    "calls": entry["calls"],
                    "servers": dict(entry["servers"]),
                }
                for identity, entry in self._callers.items()
            }


class SessionRegistry:
    """Binds each live streamable-HTTP session to one person and conversation.

    A session is keyed by ``(server, key)``, where ``key`` is the connection's
    ``Mcp-Session-Id`` or, when the transport issues none, the caller's
    conversation id. The first request on a key binds the person and
    conversation; a later request on the same key that asserts a different person
    or conversation is a conflict, and the caller closes the session with 409.
    """

    def __init__(self):
        self._sessions: dict[tuple[str, str], dict[str, str | None]] = {}
        self._lock = threading.Lock()

    def check(
        self, server: str, key: str, person: str | None, conversation: str | None
    ) -> str | None:
        """Bind the session on first sight, else compare. Return a conflict reason
        (``person_changed`` or ``conversation_changed``) or None when consistent.

        A later request that asserts no person or no conversation does not
        conflict; the bound value stands.
        """
        with self._lock:
            bound = self._sessions.get((server, key))
            if bound is None:
                self._sessions[(server, key)] = {"person": person, "conversation": conversation}
                return None
            if person is not None and bound["person"] != person:
                return "person_changed"
            if conversation is not None and bound["conversation"] != conversation:
                return "conversation_changed"
            return None

    def close(self, server: str, key: str) -> None:
        with self._lock:
            self._sessions.pop((server, key), None)

    def clear_server(self, server: str) -> None:
        """Drop every session for one server (its upstream reconnected or stopped)."""
        with self._lock:
            for pair in [p for p in self._sessions if p[0] == server]:
                del self._sessions[pair]

    def live_persons(self, server: str) -> list[str]:
        """Distinct persons with a live session on ``server``, sorted."""
        with self._lock:
            people = {
                s["person"]
                for (srv, _), s in self._sessions.items()
                if srv == server and s["person"] is not None
            }
        return sorted(people)


# Process-wide singletons (single-replica gateway → one authoritative view).
CALL_LOG = CallLog(call_log_size())
CALLERS = CallerRegistry()
SESSIONS = SessionRegistry()
