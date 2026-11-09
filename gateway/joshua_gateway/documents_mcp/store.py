"""The writes of the documents server: the frontmatter it writes, the fields a
caller sets, and the resolution of a comment.

``viewer_docstore`` is the one reader of the documents and their comments
files; this module only writes. A document is a YAML frontmatter block, then
the body. The comments of a document are one JSON object per line in
``<slug>.comments.jsonl`` beside it.
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
MAX_RESOLUTION = 300
MAX_MESSAGE = 500


class DocumentError(Exception):
    """A document operation that is not valid. The message is safe to return."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def plain(value: Any) -> Any:
    """``value`` with each date and time that YAML parsed as ISO text again."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
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


def set_resolution(comments_file: Path, comment_id: Any, resolution: str, when: str) -> None:
    """Mark the comment ``comment_id`` resolved, and keep every other line as it is."""
    out = []
    for line in comments_file.read_text(encoding="utf-8").split("\n"):
        try:
            row = json.loads(line) if line.strip() else None
        except ValueError:
            row = None
        if isinstance(row, dict) and str(row.get("id")) == str(comment_id):
            row["resolved_at"] = when
            row["resolution"] = resolution
            line = json.dumps(row, ensure_ascii=False)
        out.append(line)
    atomic_write(comments_file, "\n".join(out).encode("utf-8"))


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
