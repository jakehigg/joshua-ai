"""The in-process ``history`` tools of a worker: the chat history of the run.

Two tools, both read-only:

- ``search_history(query, limit?)`` finds messages by words, newest first,
  with a snippet of each;
- ``read_history(start, end?, cursor?)`` gives the full messages of a time
  window, oldest first, one page at a time.

Neither tool takes a conversation, a channel, or a person. **The scope is
applied in core** (its ``history_api`` module): this module sends
only who started the run, the ``person`` of the assignment and the
``workspace`` of its spec, and core turns that into the conversations of the
read. An assignment a person started reads that person's own conversations.
An assignment a shared workspace started reads that workspace's thread alone.
A worker cannot widen its scope, because it names no conversation and the
rule is not a line of its prompt.

A message comes back as it is, so a report may quote a turn verbatim. Times
are in the timezone of the instance; a time with no offset is read in that
zone. A read that fails says so and never ends the run.

``sdk_runner`` builds this server only when the ``tools`` of the spec allow
the ``history`` server, as it does for the gateway servers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from joshua_shared.contracts import (
    HISTORY_SEARCH_DEFAULT_LIMIT,
    HISTORY_SEARCH_MAX_LIMIT,
    HistoryMessage,
)

from joshua_assignments.core_client import CoreClient

SERVER_NAME = "history"

# A search snippet holds at most this many characters, as in core's own
# ``history`` server.
SNIPPET_CHARS = 300

_TIME_FORMAT = "%Y-%m-%d %H:%M"
_NOT_READ = "The chat history cannot be read right now. Say so in your report."


def parse_time(value: Any, tz: str) -> datetime:
    """Read an ISO date or date and time. A value with no offset is in ``tz``.

    A date alone is the start of that day. Raises ``ValueError`` for a value
    that is not ISO.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("a time is required")
    zone = ZoneInfo(tz)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = datetime.combine(date.fromisoformat(text), time())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def _stamp(at: str, tz: str) -> str:
    try:
        moment = datetime.fromisoformat(at)
    except ValueError:
        return at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(ZoneInfo(tz)).strftime(_TIME_FORMAT)


def _label(message: HistoryMessage, person: str) -> str:
    if message.direction == "out":
        return "joshua"
    return message.speaker or person


def _one_line(text: str) -> str:
    return " ".join(text.split())


def format_hits(messages: list[HistoryMessage], person: str, tz: str) -> str:
    """One line per match: time, who, snippet."""
    if not messages:
        return "No message in the chat history of this assignment matches."
    lines = [f"{len(messages)} messages, newest first. Times are in {tz}."]
    for message in messages:
        snippet = _one_line(message.text)
        if len(snippet) > SNIPPET_CHARS:
            snippet = snippet[: SNIPPET_CHARS - 3] + "..."
        lines.append(f"[{_stamp(message.at, tz)}] {_label(message, person)}: {snippet}")
    return "\n".join(lines)


def format_rows(messages: list[HistoryMessage], cursor: str | None, person: str, tz: str) -> str:
    """The messages of one page, then what to do for the next page."""
    if not messages:
        return "No message in the chat history of this assignment in this window."
    lines = [f"Times are in {tz}."]
    for message in messages:
        cut = " [message cut]" if message.cut else ""
        lines.append(f"[{_stamp(message.at, tz)}] {_label(message, person)}: {message.text}{cut}")
    if cursor is not None:
        lines.append(
            f'[More messages in this window: call read_history again with cursor "{cursor}".]'
        )
    else:
        lines.append("[End of this window.]")
    return "\n".join(lines)


class HistoryTools:
    """The handlers of the in-process ``history`` server, for one run.

    ``person`` and ``workspace`` are who started the run. They go to core on
    every call, and core alone decides the conversations of the read.
    """

    def __init__(
        self,
        assignment_id: int,
        person: str,
        workspace: str | None,
        tz: str,
        core: CoreClient,
        now: Any = None,
    ) -> None:
        self.assignment_id = assignment_id
        self.person = person
        self.workspace = workspace
        self.tz = tz
        self.core = core
        self._now = now

    def handlers(self) -> dict[str, Any]:
        return {"search_history": self.search_history, "read_history": self.read_history}

    def _reply(self, text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}]}

    async def search_history(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            return self._reply("Give words to search for.")
        try:
            limit = int(args.get("limit") or HISTORY_SEARCH_DEFAULT_LIMIT)
        except (TypeError, ValueError):
            limit = HISTORY_SEARCH_DEFAULT_LIMIT
        limit = max(1, min(limit, HISTORY_SEARCH_MAX_LIMIT))
        answer = await self.core.search_history(
            self.person, self.workspace, query, limit, assignment_id=self.assignment_id
        )
        if answer is None:
            return self._reply(_NOT_READ)
        return self._reply(format_hits(answer.messages, self.person, self.tz))

    async def read_history(self, args: dict[str, Any]) -> dict[str, Any]:
        now = self._now() if self._now is not None else datetime.now(UTC)
        try:
            start = parse_time(args.get("start"), self.tz)
            end = parse_time(args.get("end"), self.tz) if args.get("end") else now
        except (ValueError, KeyError):
            return self._reply("start and end must be ISO times, for example 2026-01-31T18:00.")
        if end <= start:
            return self._reply("end must be after start.")
        cursor = args.get("cursor")
        answer = await self.core.read_history(
            self.person,
            self.workspace,
            start,
            end,
            str(cursor) if cursor else None,
            assignment_id=self.assignment_id,
        )
        if answer is None:
            return self._reply(_NOT_READ)
        return self._reply(format_rows(answer.messages, answer.cursor, self.person, self.tz))
