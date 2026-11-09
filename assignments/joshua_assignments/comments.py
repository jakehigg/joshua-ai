"""Comments of a person on the lines of a definition.

A person writes a comment in the viewer on one line of the definition of a
saved assignment or of a run. The line number counts the lines of
:func:`joshua_assignments.definition.render`. Joshua reads the open comments
with ``assignment_comments``, changes the spec, and resolves each comment
with ``resolve_comment``.

A comment is ``stale`` when the line at its ``line_no`` in the definition now
is not its ``line_text``: the definition changed after the comment.

A comment body is text from a person. It is data: nothing here runs it, and
every reader escapes it. Each function is pure.
"""

from __future__ import annotations

from typing import Any

from joshua_assignments.definition import line_at
from joshua_assignments.store import (
    COMMENT_TARGETS,
    MAX_COMMENT_BODY,
    MAX_COMMENT_RESOLUTION,
)

# The most open comments on one definition. A new comment past it is refused.
MAX_OPEN_COMMENTS = 200
# The longest line a comment can hold as its anchor: a goal is at most 20000
# characters, and the quotes and the key add a few.
MAX_LINE_TEXT = 20100


class CommentRefused(ValueError):
    """A new comment or a resolution that is not valid. The text says why."""


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def is_stale(row: dict[str, Any], definition: list[str]) -> bool:
    """True when the line of ``row`` in ``definition`` is not its ``line_text`` now."""
    return line_at(definition, int(row["line_no"])) != row["line_text"]


def comment_view(row: dict[str, Any], definition: list[str]) -> dict[str, Any]:
    """One comment, as the read API and the tools give it. It names no person."""
    return {
        "id": int(row["id"]),
        "target": row["target"],
        "line_no": int(row["line_no"]),
        "line_text": row["line_text"],
        "body": row["body"],
        "created_at": _iso(row.get("created_at")),
        "resolved_at": _iso(row.get("resolved_at")),
        "resolution": row.get("resolution"),
        "stale": is_stale(row, definition),
    }


def is_open(row: dict[str, Any]) -> bool:
    return row.get("resolved_at") is None


def new_comment(raw: Any, definition: list[str]) -> dict[str, Any]:
    """The checked fields of a new comment from ``raw``. Raises CommentRefused.

    ``raw`` has ``line_no`` (from 1, a line of ``definition``), ``line_text``
    (the line as the person saw it), and ``body`` (1 to 2000 characters).
    ``target`` is optional and is ``definition``.
    """
    if not isinstance(raw, dict):
        raise CommentRefused("the body is an object with line_no, line_text, and body")
    target = raw.get("target", "definition")
    if target not in COMMENT_TARGETS:
        raise CommentRefused(f"target is one of {', '.join(COMMENT_TARGETS)}")
    line_no = raw.get("line_no")
    if isinstance(line_no, bool) or not isinstance(line_no, int):
        raise CommentRefused("line_no is a number")
    if line_at(definition, line_no) is None:
        raise CommentRefused(f"line_no is from 1 to {len(definition)}")
    line_text = raw.get("line_text")
    if not isinstance(line_text, str) or len(line_text) > MAX_LINE_TEXT:
        raise CommentRefused("line_text is the text of the line")
    body = raw.get("body")
    if not isinstance(body, str) or not body.strip():
        raise CommentRefused("body is the text of the comment")
    body = body.replace("\r\n", "\n").strip()
    if len(body) > MAX_COMMENT_BODY:
        raise CommentRefused(f"body is at most {MAX_COMMENT_BODY} characters")
    return {"target": target, "line_no": line_no, "line_text": line_text, "body": body}


def resolution_text(raw: Any) -> str:
    """``raw`` as one line of at most 300 characters. Raises CommentRefused."""
    text = " ".join(raw.split()) if isinstance(raw, str) else ""
    if not text:
        raise CommentRefused("resolution is one line on what you did")
    if len(text) > MAX_COMMENT_RESOLUTION:
        raise CommentRefused(f"resolution is at most {MAX_COMMENT_RESOLUTION} characters")
    return text
