"""The recency tier of a session's system prompt.

The block carries who the person is (their wiki profile page), verbatim.
Members also get the shared profile (the shared wiki profile page). Older
context, including the journal, does not live here — it arrives per turn
through KB injection.

:func:`build_memory_block` reads the files for one session; :func:`render`
formats the block; the composer appends it to the system prompt. The manager
builds the block once per pooled session and rebuilds when :func:`source_paths`
report a newer profile page on disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from joshua_shared import layout

_H1 = re.compile(r"^#\s+.*$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass
class MemoryBlock:
    """The recency tier for one session.

    ``shared_md`` is set for members and group sessions only.
    """

    name: str = ""
    profile_md: str = ""
    shared_md: str | None = None

    def is_empty(self) -> bool:
        return not (self.profile_md or self.shared_md)


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _clean_body(text: str) -> str:
    """Drop HTML comments and a leading H1 title, return the trimmed body.

    A profile page opens with a display-name title and a template comment;
    the block renders its own heading, so both go.
    """
    text = _COMMENT.sub("", text)
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and _H1.match(lines[i]):
        i += 1
    return "\n".join(lines[i:]).strip()


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
    rebuild after the nightly rewrite of a profile page.
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
    shared_max_chars: int = 2000,
    data_dir: Path | str | None = None,
) -> MemoryBlock | None:
    """Build the recency tier for a session, or None when there is nothing.

    A group session gets only the shared section. An unidentified per-person
    session (``person is None``) gets no block. A guest gets their own
    profile without the shared section; a member gets both.
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
    if block.shared_md:
        parts.append(f"## Shared profile\n\n{block.shared_md}")
    return "\n\n".join(parts).strip()
