"""The rules and the records of a transcript search and a transcript read.

``Repo.search_transcript`` and ``Repo.read_transcript`` run the SQL. This module
holds what the SQL and a test fake must agree on: which rows a reader sees, the
text search configuration, the page limits, and the cursor.

Each query takes an explicit collection of allowed conversation ids, and a row
of any other conversation is never returned. The ``history`` tool of core
passes the id of the current conversation only. An external reader, such as
an MCP server for a person, can pass the ids that its caller may read, and use
the same functions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

# A reader sees a message from a person (``in``) and a reply (``out``) only.
# A scheduled task prompt (``task``) and an event text (``inject``) are input
# to the agent, not a message of the conversation.
VISIBLE_DIRECTIONS: tuple[str, ...] = ("in", "out")
# A failed turn writes an ``out`` row with this status. Its text is an error,
# not a message that a person got.
FAILED_STATUS = "failed"
# The reply that tells delivery to send nothing (``delivery.IGNORE_MARKER``).
SILENT_REPLY = "[IGNORE]"
# The characters that ``is_visible`` and the SQL ``btrim`` remove from each end.
BLANK_CHARS = " \t\r\n"

# The text search configuration of the transcript. The GIN index in
# ``SCHEMA_SQL`` uses the same one, so the two must stay equal.
TS_CONFIG = "english"
# The ``ts_headline`` options of a search snippet.
HEADLINE_OPTIONS = 'MaxWords=24, MinWords=8, MaxFragments=2, FragmentDelimiter=" ... "'


@dataclass(frozen=True)
class TranscriptHit:
    """One search result: a snippet of one message."""

    id: int
    conversation_id: str
    direction: str
    speaker: str | None
    created_at: datetime
    snippet: str


@dataclass(frozen=True)
class TranscriptRow:
    """One message of a read. ``cut`` is True when ``content`` is shorter
    than the message."""

    id: int
    conversation_id: str
    direction: str
    speaker: str | None
    created_at: datetime
    content: str
    cut: bool = False


@dataclass(frozen=True)
class TranscriptPage:
    """The rows of one read, and the cursor of the next page or None."""

    rows: list[TranscriptRow]
    cursor: str | None


def is_visible(direction: str, status: str | None, content: str | None) -> bool:
    """True for a row that a reader of the transcript sees."""
    if direction not in VISIBLE_DIRECTIONS or status == FAILED_STATUS:
        return False
    text = (content or "").strip(BLANK_CHARS)
    return bool(text) and text != SILENT_REPLY


def encode_cursor(row_id: int) -> str:
    """The cursor that continues a read after the row ``row_id``."""
    return str(row_id)


def decode_cursor(cursor: str | None) -> int | None:
    """The row id in ``cursor``, or None for no cursor. Raises ``ValueError``
    for a cursor that is not one from ``encode_cursor``."""
    if cursor is None or cursor == "":
        return None
    text = str(cursor).strip()
    if not text.isdigit():
        raise ValueError("cursor is not valid")
    return int(text)


def page_rows(rows: Sequence[TranscriptRow], *, max_rows: int, max_chars: int) -> TranscriptPage:
    """Cut ``rows`` (in read order) to one page.

    A page holds at most ``max_rows`` rows and at most ``max_chars`` characters
    of content. A row that alone is longer than ``max_chars`` is cut and
    marked ``cut``, so each page holds at least one row. The cursor is set
    when rows remain after the page.
    """
    page: list[TranscriptRow] = []
    used = 0
    for row in rows:
        if len(page) >= max_rows:
            break
        size = len(row.content)
        if page and used + size > max_chars:
            break
        if not page and size > max_chars:
            row = replace(row, content=row.content[:max_chars], cut=True)
            size = max_chars
        page.append(row)
        used += size
    more = len(page) < len(rows)
    cursor = encode_cursor(page[-1].id) if more and page else None
    return TranscriptPage(rows=page, cursor=cursor)
