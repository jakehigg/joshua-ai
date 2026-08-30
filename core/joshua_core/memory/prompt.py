"""The recency tier of a session's system prompt.

The block carries who the person is (``profile.md``) and what happened in the
last few days (the newest blog posts), verbatim. Members also get the shared
profile (``shared/profile.md``). Older context does not live here — it arrives
per turn through KB injection.

:func:`build_memory_block` reads the files for one session; :func:`render`
formats the block; the composer appends it to the system prompt. The manager
builds the block once per pooled session and rebuilds when :func:`source_paths`
report a newer ``profile.md`` or ``shared/profile.md`` on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from joshua_shared import layout

_H1 = re.compile(r"^#\s+.*$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOG_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


@dataclass
class MemoryBlock:
    """The recency tier for one session.

    ``recent_posts`` is newest first, each entry a ``(date, markdown)`` pair.
    ``shared_md`` is set for members and group sessions only.
    """

    name: str = ""
    profile_md: str = ""
    recent_posts: list[tuple[date, str]] = field(default_factory=list)
    shared_md: str | None = None

    def is_empty(self) -> bool:
        return not (self.profile_md or self.recent_posts or self.shared_md)


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _clean_body(text: str) -> str:
    """Drop HTML comments and a leading H1 title, return the trimmed body.

    ``profile.md`` and ``shared/profile.md`` open with a display-name title and a
    template comment; the block renders its own heading, so both go.
    """
    text = _COMMENT.sub("", text)
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and _H1.match(lines[i]):
        i += 1
    return "\n".join(lines[i:]).strip()


def _strip_frontmatter(text: str) -> str:
    """Remove a leading ``---`` YAML frontmatter block from a blog post body."""
    if not text.startswith("---"):
        return text
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                return "\n".join(lines[i + 1 :]).lstrip("\n")
    return text


def _date_from_name(name: str) -> date | None:
    m = _BLOG_DATE.match(name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _recent_posts(pid: str, n: int, data_dir: Path | str | None) -> list[tuple[date, str]]:
    """The ``n`` most recent blog dates, newest first.

    A date's posts (the nightly digest and any agent posts) are joined in name
    order under one date.
    """
    if n <= 0:
        return []
    blog = layout.person_dir(pid, "blog", data_dir)
    by_date: dict[date, list[tuple[str, Path]]] = {}
    try:
        entries = list(blog.iterdir())
    except OSError:
        return []
    for path in entries:
        if not path.is_file() or path.suffix != ".md":
            continue
        d = _date_from_name(path.name)
        if d is None:
            continue
        by_date.setdefault(d, []).append((path.name, path))
    out: list[tuple[date, str]] = []
    for d in sorted(by_date, reverse=True)[:n]:
        bodies = [_strip_frontmatter(_read(p)).strip() for _, p in sorted(by_date[d])]
        out.append((d, "\n\n".join(b for b in bodies if b)))
    return out


def _fit_posts(posts: list[tuple[date, str]], max_chars: int) -> list[tuple[date, str]]:
    """Keep the whole recent-post list, but truncate the oldest bodies first.

    The newest posts fill the budget in full; the post that crosses the limit is
    cut, and older ones keep only their date heading.
    """
    if max_chars <= 0:
        return posts
    out: list[tuple[date, str]] = []
    budget = max_chars
    for d, body in posts:  # newest first
        if budget <= 0:
            out.append((d, ""))
        elif len(body) <= budget:
            out.append((d, body))
            budget -= len(body)
        else:
            out.append((d, body[:budget].rstrip()))
            budget = 0
    return out


def _shared_md(max_chars: int, data_dir: Path | str | None) -> str | None:
    body = _clean_body(_read(layout.shared_profile_path(data_dir)))
    if not body:
        return None
    return body[:max_chars] if max_chars > 0 else body


def _is_group(channel: Any) -> bool:
    return channel is not None and getattr(channel, "session_mode", None) == "shared"


def source_paths(person: Any, channel: Any, data_dir: Path | str | None = None) -> list[Path]:
    """The files that back a session's memory block.

    The manager compares their newest mtime against the session build time to
    rebuild after the nightly rewrite of ``profile.md`` or ``shared/profile.md``.
    """
    if _is_group(channel):
        return [layout.shared_profile_path(data_dir)]
    if person is None:
        return []
    paths = [layout.profile_path(person.id, data_dir)]
    if getattr(person, "role", None) != "guest":
        paths.append(layout.shared_profile_path(data_dir))
    return paths


def newest_mtime(paths: list[Path]) -> float:
    newest = 0.0
    for path in paths:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def build_memory_block(
    person: Any,
    channel: Any,
    *,
    recent_posts: int = 3,
    recent_max_chars: int = 6000,
    shared_max_chars: int = 2000,
    data_dir: Path | str | None = None,
) -> MemoryBlock | None:
    """Build the recency tier for a session, or None when there is nothing.

    A group session gets only the shared section. An unidentified per-person
    session (``person is None``) gets no block. A guest gets their own profile
    and posts without the shared section; a member gets both.
    """
    if _is_group(channel):
        shared = _shared_md(shared_max_chars, data_dir)
        return MemoryBlock(shared_md=shared) if shared else None
    if person is None:
        return None
    shared = None
    if getattr(person, "role", None) != "guest":
        shared = _shared_md(shared_max_chars, data_dir)
    block = MemoryBlock(
        name=getattr(person, "display_name", "") or person.id,
        profile_md=_clean_body(_read(layout.profile_path(person.id, data_dir))),
        recent_posts=_fit_posts(_recent_posts(person.id, recent_posts, data_dir), recent_max_chars),
        shared_md=shared,
    )
    return None if block.is_empty() else block


def render(block: MemoryBlock) -> str:
    """Format a :class:`MemoryBlock` for the system prompt. Returns "" when empty."""
    parts: list[str] = []
    if block.profile_md:
        parts.append(
            f"## About {block.name} (your profile of them — maintained nightly)\n\n"
            f"{block.profile_md}"
        )
    if block.recent_posts:
        days = ["## Recent days"]
        for d, body in block.recent_posts:
            days.append(f"### {d.isoformat()}\n\n{body}".rstrip())
        parts.append("\n\n".join(days))
    if block.shared_md:
        parts.append(f"## Shared profile\n\n{block.shared_md}")
    return "\n\n".join(parts).strip()
