"""One document on disk: its frontmatter, its body, and its comments file.

A document is ``documents/<owner>/<slug>.md`` (or ``.html``): a YAML
frontmatter block, then the body. The comments of a document are one JSON
object per line in ``<slug>.comments.jsonl`` beside it. Every function here
works on text or on one path; ``server`` checks the access first.

A comment anchors to a line of the body: ``line_no`` counts from 1 at the
first line after the closing ``---`` of the frontmatter, and ``line_text`` is
that line as the person saw it. A comment is ``stale`` when the line at
``line_no`` is not ``line_text`` now: the body changed after the comment.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

KINDS = ("plan", "report", "spec", "artifact", "note")
STATUSES = ("draft", "active", "final", "archived")
FORMATS = ("markdown", "html")

# The frontmatter keys in the order they are written.
FIELD_ORDER = (
    "title",
    "kind",
    "status",
    "format",
    "tags",
    "summary",
    "author",
    "assignment",
    "created",
    "updated",
    "promoted_to",
)

MAX_TITLE = 200
MAX_SUMMARY = 300
MAX_TAGS = 20
MAX_TAG = 40
MAX_COMMENT_BODY = 2000
MAX_RESOLUTION = 300
MAX_MESSAGE = 500
# A summary made from the first paragraph of the body is cut at this length.
FIRST_PARAGRAPH_CHARS = 300


class DocumentError(Exception):
    """A document operation that is not valid. The message is safe to return."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def split(text: str) -> tuple[dict[str, Any], str]:
    """Split document ``text`` into its frontmatter and its body.

    The body is everything after the line of the closing ``---``. Text with no
    frontmatter block is all body. A date or a time that YAML parses becomes
    its ISO text again.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 3)
    if end == -1:
        if text.endswith("\n---"):
            end = len(text) - 4
        else:
            return {}, text
    block = text[4:end]
    body = text[end + 5 :]
    try:
        data = yaml.safe_load(block) if block.strip() else {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return {str(key): _plain(value) for key, value in data.items()}, body


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def join(meta: dict[str, Any], body: str) -> str:
    """The document text for ``meta`` and ``body``. Known keys go first, in order."""
    ordered = {key: meta[key] for key in FIELD_ORDER if meta.get(key) is not None}
    for key, value in meta.items():
        if key not in ordered and value is not None:
            ordered[key] = value
    block = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True)
    return f"---\n{block}---\n{body}"


def has_frontmatter(body: str) -> bool:
    """True when ``body`` opens with a closed ``---`` block."""
    lines = body.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    return any(line.strip() == "---" for line in lines[1:])


def first_paragraph(body: str) -> str:
    """The first paragraph of a markdown body that is not a heading, on one line."""
    for block in body.replace("\r\n", "\n").split("\n\n"):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        lines = [line for line in lines if not line.startswith("#")]
        if not lines or lines[0] in ("---", "```"):
            continue
        text = " ".join(" ".join(lines).split())
        return text[:FIRST_PARAGRAPH_CHARS]
    return ""


def summary_of(meta: dict[str, Any], body: str) -> str:
    """The ``summary`` of a document, else the first paragraph of a markdown body."""
    summary = meta.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    if meta.get("format", "markdown") == "html":
        return ""
    return first_paragraph(body)


# -- the fields a caller sets ------------------------------------------------


def one_line(value: Any, name: str, limit: int, *, required: bool = False) -> str | None:
    """``value`` as one trimmed line of at most ``limit`` characters."""
    if value is None:
        if required:
            raise DocumentError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise DocumentError(f"{name} is text")
    text = " ".join(value.split())
    if not text:
        if required:
            raise DocumentError(f"{name} is required")
        return None
    if len(text) > limit:
        raise DocumentError(f"{name} is at most {limit} characters")
    return text


def choice(value: Any, name: str, options: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    if value not in options:
        raise DocumentError(f"{name} is one of {', '.join(options)}")
    return str(value)


def tags_of(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(tag, str) for tag in value):
        raise DocumentError("tags is a list of words")
    tags = [" ".join(tag.split()) for tag in value]
    tags = [tag for tag in tags if tag]
    if len(tags) > MAX_TAGS:
        raise DocumentError(f"at most {MAX_TAGS} tags")
    if any(len(tag) > MAX_TAG for tag in tags):
        raise DocumentError(f"a tag is at most {MAX_TAG} characters")
    return list(dict.fromkeys(tags))


# -- comments ----------------------------------------------------------------


def read_comments(comments_file: Path) -> list[dict[str, Any]]:
    """Every comment in ``comments_file``, in file order. A line that is not a JSON
    object with an id is skipped."""
    if not comments_file.is_file():
        return []
    comments = []
    for line in comments_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "id" in row:
            comments.append(row)
    return comments


def write_comments(comments_file: Path, comments: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in comments)
    atomic_write(comments_file, text.encode("utf-8"))


def is_open(row: dict[str, Any]) -> bool:
    return not row.get("resolved_at")


def line_at(lines: list[str], line_no: Any) -> str | None:
    if isinstance(line_no, bool) or not isinstance(line_no, int):
        return None
    if 1 <= line_no <= len(lines):
        return lines[line_no - 1]
    return None


def comment_view(row: dict[str, Any], body: str) -> dict[str, Any]:
    """One comment as the tools give it, with ``stale`` and ``line_now`` computed now."""
    line_now = line_at(body.splitlines(), row.get("line_no"))
    return {
        "id": row.get("id"),
        "line_no": row.get("line_no"),
        "line_text": row.get("line_text"),
        "body": row.get("body"),
        "author": row.get("author"),
        "created_at": row.get("created_at"),
        "resolved_at": row.get("resolved_at"),
        "resolution": row.get("resolution"),
        "stale": line_now != row.get("line_text"),
        "line_now": line_now,
    }


def open_count(comments_file: Path) -> int:
    return sum(1 for row in read_comments(comments_file) if is_open(row))


# -- disk --------------------------------------------------------------------


def atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically, creating the parent folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
