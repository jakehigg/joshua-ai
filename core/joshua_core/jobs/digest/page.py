"""The digest entry, and the one frontmatter line on each entry it covers.

A digest entry is a journal entry on the day of the last entry it covers, with
the slug `digest-<key>`, and a number after it when that slug is taken. Its
frontmatter holds `date`, `people` (each person of a covered entry), `source:
digest`, `covers` (the covered paths, relative to the wiki root), and
`digest_run` (the run id). It has no `run_id`, because a digest is memory and
the index keeps it.

`mark` adds one line, `digest: <digest path>`, to the frontmatter of a covered
entry. The body and every other line stay byte for byte. `unmark` removes that
line again, and gives the text that the entry had before `mark`.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Collection
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from joshua_shared import frontmatter, layout

from joshua_core.jobs.digest.groups import COVERED_KEY, DIGEST_SOURCE, Group

SLUG_PREFIX = "digest-"
_MAX_SLUG = 64
_NOT_SLUG_RE = re.compile(r"[^a-z0-9]+")


def digest_slug(key: str) -> str:
    """`digest-<key>`, as a journal slug of 64 characters at most."""
    clean = _NOT_SLUG_RE.sub("-", key.lower()).strip("-") or "entries"
    return (SLUG_PREFIX + clean)[:_MAX_SLUG].rstrip("-")


def digest_path(group: Group, data_root: Path, taken: Collection[Path] = ()) -> Path:
    """The path of the digest of `group`: on the day of its last entry, with
    a slug that no file and no path in `taken` holds."""
    base = digest_slug(group.key)
    n = 1
    while True:
        slug = base if n == 1 else f"{base[: _MAX_SLUG - len(str(n)) - 1]}-{n}"
        path = layout.journal_entry_path(group.last, slug, data_root)
        if not path.exists() and path not in taken:
            return path
        n += 1


def people_of(group: Group) -> list[str]:
    """Each person of the covered entries, once, in sorted order."""
    return sorted({p for entry in group.entries for p in entry.people})


def render_digest(
    *,
    group: Group,
    digest_rel: str,
    title: str,
    summary: str,
    run_id: int | None,
) -> str:
    """The full text of the digest entry of `group`. `digest_rel` is its own
    path relative to the wiki root."""
    meta: dict[str, Any] = {
        "date": group.last,
        "people": people_of(group),
        "source": DIGEST_SOURCE,
        "covers": group.paths,
    }
    if run_id is not None:
        meta["digest_run"] = run_id
    block = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    here = posixpath.dirname(digest_rel)
    links = "\n".join(
        f"- [{e.day.isoformat()} {e.slug}]({posixpath.relpath(e.rel, here)})" for e in group.entries
    )
    span = _span(group.first, group.last)
    return (
        f"---\n{block}---\n\n# {title}\n\n{summary.strip()}\n\n"
        f"Covers {group.size} entries from {span}:\n\n{links}\n"
    )


def _span(first: date, last: date) -> str:
    return f"{first.isoformat()} to {last.isoformat()}"


def mark(text: str, digest_rel: str) -> str:
    """`text` with the one line `digest: <digest_rel>` added to its
    frontmatter. Raises `ValueError` when the block cannot take a key."""
    return frontmatter.update(text, **{COVERED_KEY: digest_rel})


def unmark(text: str, digest_rel: str) -> str | None:
    """`text` without the `digest:` line that names `digest_rel`, or None when
    it has no such line. When that line was the whole block, the block goes
    too, because `mark` added it."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    close = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---"), None)
    if close is None:
        return None
    for i in range(1, close):
        line = lines[i]
        if not line.startswith(f"{COVERED_KEY}:"):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if i + 1 < close and (nxt[:1].isspace() or nxt.startswith("-")):
            continue
        try:
            value = yaml.safe_load(line)
        except yaml.YAMLError:
            continue
        if value != {COVERED_KEY: digest_rel}:
            continue
        if close == 2:
            return "".join(lines[3:])
        return "".join(lines[:i] + lines[i + 1 :])
    return None


__all__ = [
    "SLUG_PREFIX",
    "digest_path",
    "digest_slug",
    "mark",
    "people_of",
    "render_digest",
    "unmark",
]
