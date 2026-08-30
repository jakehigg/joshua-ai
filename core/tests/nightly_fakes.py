"""Offline fakes for the nightly-reflection tests.

They stand in for the store repo, the memory indexer, and the conversation
manager so the reflector runs with no DB, no SDK, and no network. A canned
``oneshot`` returns a JSON reflection without a model call.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from joshua_core.store.models import Person
from joshua_shared import config as config_module

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    role: member
  - id: gwen
    name: Gwen
    role: guest
"""


def make_settings(extra: str = "") -> Any:
    return config_module.parse(CONFIG + extra, env={}, source="<test>")


def canned_oneshot(
    *, post: str = "Alex had a good day.", profile: str | None = "# Alex\n## About\nLikes tea."
) -> Any:
    """A oneshot that answers the person call with ``{post, profile}`` and the
    shared-profile call with a fixed shared profile."""

    async def _oneshot(*, system_prompt: str, user_prompt: str, model: str | None) -> str:
        if "shared profile" in system_prompt:
            return json.dumps({"profile": "# House\n## About\nQuiet weeknights."})
        return json.dumps({"post": post, "profile": profile})

    return _oneshot


def junk_oneshot() -> Any:
    async def _oneshot(*, system_prompt: str, user_prompt: str, model: str | None) -> str:
        return "not json at all"

    return _oneshot


class FakeIndexer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def reindex(
        self, *, source: str | None = None, person: str | None = None, full: bool = False
    ) -> dict[str, Any]:
        self.calls.append({"source": source, "person": person})
        return {}


class FakeManager:
    def __init__(self) -> None:
        self.flushed = 0

    async def flush_sessions(self) -> int:
        self.flushed += 1
        return 0


class FakeReflectRepo:
    """Serves the roster, the day's transcript, and conversation metadata.

    ``conversations`` (optional) drives ``rollover_sessions``: each entry is
    ``{cid: {"session_started_on": date|None, "sdk_session_id": str|None}}``.
    """

    def __init__(
        self,
        *,
        people: list[Person],
        rows: list[dict[str, Any]] | None = None,
        conv_meta: dict[str, dict[str, Any]] | None = None,
        conversations: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._people = people
        self._rows = rows or []
        self._conv_meta = conv_meta or {}
        self.conversations = conversations or {}

    async def list_people(self) -> list[Person]:
        return list(self._people)

    async def recent_transcripts(self, since: Any) -> list[dict[str, Any]]:
        return [r for r in self._rows if r["created_at"] >= since]

    async def read_conversation_meta(self, cid: str) -> dict[str, Any] | None:
        return self._conv_meta.get(cid)

    async def rollover_sessions(self, today: date) -> int:
        rolled = 0
        for state in self.conversations.values():
            started = state.get("session_started_on")
            if started is None or started < today:
                state["session_started_on"] = today
                state["sdk_session_id"] = None
                rolled += 1
        return rolled


def row(
    cid: str,
    direction: str,
    content: str,
    when: Any,
    *,
    turn_id: str = "t1",
    speaker: str | None = None,
    tools: list[str] | None = None,
    written: list[str] | None = None,
    status: str = "ok",
) -> dict[str, Any]:
    meta: dict[str, Any] = {"turn_id": turn_id}
    if speaker:
        meta["speaker"] = speaker
    if tools:
        meta["tools"] = tools
    if written:
        meta["written"] = written
    return {
        "conversation_id": cid,
        "direction": direction,
        "content": content,
        "status": status,
        "meta": meta,
        "created_at": when,
    }
