"""In-process ``history`` tool server: the transcript of the current conversation.

Two tools, both read-only and both bound to ``ToolDeps.conversation``:

- ``search_history(query, limit?)`` finds messages by words, newest first,
  with a snippet of each.
- ``read_history(start, end?, cursor?)`` gives the full messages of a time
  window, oldest first, one page at a time.

Neither tool takes a conversation, a channel, or a person. The scope is the
conversation that the session serves (that direct message, that group, that
voice thread), and the store query gets that one id only. Times are in
``ToolDeps.tz``; a time with no offset is read in that zone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.tools import ToolDeps
from joshua_core.store.transcript import TranscriptHit, TranscriptRow

SERVER_NAME = "history"

SEARCH_DEFAULT_LIMIT = 10
SEARCH_MAX_LIMIT = 50
# A search snippet holds at most this many characters.
SNIPPET_CHARS = 300
# One read gives at most this many messages and this many characters of text.
READ_MAX_ROWS = 60
READ_MAX_CHARS = 12_000

_TIME_FORMAT = "%Y-%m-%d %H:%M"


def _reply(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


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


def _label(direction: str, speaker: str | None, deps: ToolDeps) -> str:
    if direction == "out":
        return "you"
    return speaker or deps.conversation.person_id or "person"


def _stamp(at: datetime, tz: str) -> str:
    return at.astimezone(ZoneInfo(tz)).strftime(_TIME_FORMAT)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def format_hits(hits: list[TranscriptHit], deps: ToolDeps) -> str:
    """One line per hit: time, who, snippet."""
    if not hits:
        return "No message in this conversation matches."
    lines = [f"{len(hits)} messages, newest first. Times are in {deps.tz}."]
    for hit in hits:
        snippet = _one_line(hit.snippet)
        if len(snippet) > SNIPPET_CHARS:
            snippet = snippet[: SNIPPET_CHARS - 3] + "..."
        who = _label(hit.direction, hit.speaker, deps)
        lines.append(f"[{_stamp(hit.created_at, deps.tz)}] {who}: {snippet}")
    return "\n".join(lines)


def format_rows(rows: list[TranscriptRow], cursor: str | None, deps: ToolDeps) -> str:
    """The messages of one page, then what to do for the next page."""
    if not rows:
        return "No message in this conversation in this window."
    lines = [f"Times are in {deps.tz}."]
    for row in rows:
        who = _label(row.direction, row.speaker, deps)
        cut = " [message cut]" if row.cut else ""
        lines.append(f"[{_stamp(row.created_at, deps.tz)}] {who}: {row.content}{cut}")
    if cursor is not None:
        lines.append(
            f'[More messages in this window: call read_history again with cursor "{cursor}".]'
        )
    else:
        lines.append("[End of this window.]")
    return "\n".join(lines)


async def do_search_history(deps: ToolDeps, *, query: str, limit: Any = None) -> str:
    """Search the current conversation. ``limit`` is cut to 1..``SEARCH_MAX_LIMIT``."""
    query = (query or "").strip()
    if not query:
        return "Give words to search for."
    try:
        n = int(limit) if limit else SEARCH_DEFAULT_LIMIT
    except (TypeError, ValueError):
        n = SEARCH_DEFAULT_LIMIT
    n = max(1, min(n, SEARCH_MAX_LIMIT))
    hits = await deps.repo.search_transcript([deps.conversation.id], query, limit=n)
    return format_hits(hits, deps)


async def do_read_history(
    deps: ToolDeps,
    *,
    start: Any,
    end: Any = None,
    cursor: Any = None,
    now: datetime | None = None,
) -> str:
    """Read the current conversation from ``start`` to ``end`` (default now)."""
    try:
        start_at = parse_time(start, deps.tz)
        end_at = parse_time(end, deps.tz) if end else (now or datetime.now(UTC))
    except (ValueError, KeyError):
        return "start and end must be ISO times, for example 2026-01-31T18:00."
    if end_at <= start_at:
        return "end must be after start."
    try:
        page = await deps.repo.read_transcript(
            [deps.conversation.id],
            start_at,
            end_at,
            cursor=str(cursor) if cursor else None,
            max_rows=READ_MAX_ROWS,
            max_chars=READ_MAX_CHARS,
        )
    except ValueError:
        return "The cursor is not valid. Use the cursor from the last read_history result."
    return format_rows(page.rows, page.cursor, deps)


_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": 'Words to find. "a phrase", or, and -word also work.',
        },
        "limit": {
            "type": "integer",
            "description": f"Maximum results (default {SEARCH_DEFAULT_LIMIT}, "
            f"most {SEARCH_MAX_LIMIT}).",
        },
    },
    "required": ["query"],
}

_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "description": "ISO time; the window starts here."},
        "end": {"type": "string", "description": "ISO time; the window ends here. Default: now."},
        "cursor": {"type": "string", "description": "The cursor from the last result."},
    },
    "required": ["start"],
}


def build_history_server(deps: ToolDeps) -> Any:
    """Build the in-process ``history`` MCP server bound to one conversation."""

    @tool(
        "search_history",
        "Search the messages of this conversation only, by words. Gives the time, "
        "who wrote it, and a snippet, newest first.",
        _SEARCH_SCHEMA,
    )
    async def search_history(args: dict[str, Any]) -> dict[str, Any]:
        text = await do_search_history(deps, query=args.get("query", ""), limit=args.get("limit"))
        return _reply(text)

    @tool(
        "read_history",
        "Read the full messages of this conversation only, for a time window. "
        "Times with no offset are local. A long window comes in pages: pass the "
        "cursor of the last result for the next page.",
        _READ_SCHEMA,
    )
    async def read_history(args: dict[str, Any]) -> dict[str, Any]:
        text = await do_read_history(
            deps, start=args.get("start"), end=args.get("end"), cursor=args.get("cursor")
        )
        return _reply(text)

    return create_sdk_mcp_server(
        name=SERVER_NAME, version="1.0.0", tools=[search_history, read_history]
    )


def register(manager: Any) -> None:
    """Wire the ``history`` builtin into the conversation manager."""
    manager.register_builtin(SERVER_NAME, build_history_server)
