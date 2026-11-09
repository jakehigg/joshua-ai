"""Comments of a person on the lines of a definition, a plan, or a report.

A person writes a comment in the viewer on one line of a text, its
``target``: the definition of a saved assignment or of a run, or the plan or
the report of a run. The line number of a definition counts the lines of
:func:`joshua_assignments.definition.render`; the line number of a plan or a
report counts the lines of the stored text. Joshua reads the open comments
with ``assignment_comments``, applies them, and resolves each comment with
``resolve_comment``.

A comment is ``stale`` when the line at its ``line_no`` in its target now is
not its ``line_text``: the text changed after the comment.

A comment body is text from a person. It is data: nothing here runs it, and
every reader escapes it. Each function is pure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from joshua_shared.ids import PERSON_ID_RE

from joshua_assignments import definition as definition_mod
from joshua_assignments.definition import line_at
from joshua_assignments.spec import SAVED_NAME_RE
from joshua_assignments.store import (
    COMMENT_TARGETS,
    MAX_APPLIED,
    MAX_APPLIED_REF,
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


class NothingApplied(CommentRefused):
    """A resolution with no reference of what it changed."""


# The references that ``applied`` takes. A document path is
# ``documents/<owner>/<slug>.md`` (or ``.html``); ``owner`` is a person id or
# ``shared``.
_DOCUMENT_RE = re.compile(
    r"^/?documents/(?P<owner>[^/\s]+)/(?P<slug>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.(md|html)\Z"
)
_TASK_RE = re.compile(r"^task\s+#?(?P<n>[0-9]{1,9})\Z", re.IGNORECASE)
_ASSIGNMENT_RE = re.compile(r"^assignment\s+#?(?P<n>[0-9]{1,18})\Z", re.IGNORECASE)
_SAVED_RE = re.compile(r"^saved\s+(?P<name>\S+)\Z", re.IGNORECASE)
APPLIED_KINDS = (
    "a document path (documents/<owner>/<slug>.md), a task (task 78), an issue URL, "
    "assignment N (a draft or a run whose spec changed), or saved <name>"
)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def text_lines(text: str | None) -> list[str]:
    """The lines of a plan or a report, as the viewer counts them."""
    return text.replace("\r\n", "\n").split("\n") if text else []


def texts(parent: Mapping[str, Any]) -> dict[str, list[str]]:
    """The lines of each target of ``parent``, a saved assignment or a run.

    A saved assignment has a definition only."""
    out = {"definition": definition_mod.lines(parent.get("spec") or {})}
    if "plan" in parent or "report" in parent:
        out["plan"] = text_lines(parent.get("plan"))
        out["report"] = text_lines(parent.get("report"))
    return out


def _lines_of(row: Mapping[str, Any], lines: Mapping[str, list[str]]) -> list[str]:
    return lines.get(str(row.get("target") or "definition"), [])


def line_now(row: Mapping[str, Any], lines: Mapping[str, list[str]]) -> str | None:
    """The line of ``row`` in its target now, or None when the line is gone."""
    return line_at(_lines_of(row, lines), int(row["line_no"]))


def is_stale(row: Mapping[str, Any], lines: Mapping[str, list[str]]) -> bool:
    """True when the line of ``row`` in its target is not its ``line_text`` now.

    ``lines`` holds the lines of each target, as :func:`texts` gives them."""
    return line_now(row, lines) != row["line_text"]


def comment_view(row: dict[str, Any], lines: Mapping[str, list[str]]) -> dict[str, Any]:
    """One comment, as the read API and the tools give it. It names no person."""
    now = line_now(row, lines)
    return {
        "id": int(row["id"]),
        "target": row["target"],
        "line_no": int(row["line_no"]),
        "line_text": row["line_text"],
        "line_now": now,
        "body": row["body"],
        "created_at": _iso(row.get("created_at")),
        "resolved_at": _iso(row.get("resolved_at")),
        "resolution": row.get("resolution"),
        "applied": list(row.get("applied") or []),
        "stale": now != row["line_text"],
    }


def is_open(row: dict[str, Any]) -> bool:
    return row.get("resolved_at") is None


def new_comment(raw: Any, lines: Mapping[str, list[str]]) -> dict[str, Any]:
    """The checked fields of a new comment from ``raw``. Raises CommentRefused.

    ``raw`` has ``line_no`` (from 1, a line of its target), ``line_text``
    (the line as the person saw it), and ``body`` (1 to 2000 characters).
    ``target`` is optional: ``definition`` (the default), ``plan``, or
    ``report``. ``lines`` holds the lines of each target that the parent
    has, as :func:`texts` gives them.
    """
    if not isinstance(raw, dict):
        raise CommentRefused("the body is an object with line_no, line_text, and body")
    target = raw.get("target", "definition")
    if target not in COMMENT_TARGETS:
        raise CommentRefused(f"target is one of {', '.join(COMMENT_TARGETS)}")
    if target not in lines:
        raise CommentRefused(f"a saved assignment has no {target}: its target is definition")
    text = lines[target]
    if not text:
        raise CommentRefused(f"the {target} is empty")
    line_no = raw.get("line_no")
    if isinstance(line_no, bool) or not isinstance(line_no, int):
        raise CommentRefused("line_no is a number")
    if line_at(text, line_no) is None:
        raise CommentRefused(f"line_no is from 1 to {len(text)}")
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


def applied_ref(raw: Any) -> str | None:
    """``raw`` as one reference of ``applied``, in its plain form, or None.

    ``task #78`` becomes ``task 78``, and a leading ``/`` of a document path
    goes."""
    text = raw.strip() if isinstance(raw, str) else ""
    if not text or len(text) > MAX_APPLIED_REF:
        return None
    document = _DOCUMENT_RE.match(text)
    if document is not None:
        owner, slug = document.group("owner"), document.group("slug")
        if (owner == "shared" or PERSON_ID_RE.match(owner)) and ".." not in slug:
            return text.lstrip("/")
        return None
    for pattern, kind in ((_TASK_RE, "task"), (_ASSIGNMENT_RE, "assignment")):
        found = pattern.match(text)
        if found is not None:
            return f"{kind} {int(found.group('n'))}"
    saved = _SAVED_RE.match(text)
    if saved is not None:
        name = saved.group("name").lower()
        return f"saved {name}" if SAVED_NAME_RE.match(name) else None
    if any(c.isspace() for c in text):
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return text
    return None


def applied_refs(raw: Any) -> list[str]:
    """``raw`` as the list of what a resolution changed. Raises CommentRefused.

    ``raw`` is a list of 1 to :data:`MAX_APPLIED` references, each one of
    :data:`APPLIED_KINDS`. An empty or missing list raises
    :class:`NothingApplied`. A reference that comes two times is kept once."""
    if raw is None or (isinstance(raw, list) and not raw):
        raise NothingApplied(
            f"applied names what you changed, at least one reference: {APPLIED_KINDS}"
        )
    if not isinstance(raw, list):
        raise CommentRefused("applied is a list of references")
    if len(raw) > MAX_APPLIED:
        raise CommentRefused(f"applied holds at most {MAX_APPLIED} references")
    out: list[str] = []
    for item in raw:
        ref = applied_ref(item)
        if ref is None:
            raise CommentRefused(f"each reference of applied is {APPLIED_KINDS}")
        if ref not in out:
            out.append(ref)
    return out


def resolution_text(raw: Any) -> str:
    """``raw`` as one line of at most 300 characters. Raises CommentRefused."""
    text = " ".join(raw.split()) if isinstance(raw, str) else ""
    if not text:
        raise CommentRefused("resolution is one line on what you did")
    if len(text) > MAX_COMMENT_RESOLUTION:
        raise CommentRefused(f"resolution is at most {MAX_COMMENT_RESOLUTION} characters")
    return text
