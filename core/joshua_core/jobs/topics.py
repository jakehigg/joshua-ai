"""Topics: the leaf pages of the wiki that a job works on one at a time.

A topic is a `wiki/**.md` page that is none of these:

- `Home.md`, the front page;
- a section index page: `X.md` with a folder `X/` beside it;
- a page under `journal/`, `people/`, `joshua-docs/`, `skills/`,
  `knowledge/`, `insights/`, or `attachments/`;
- a page with a part of its path that starts with a dot;
- a page that a glob in `jobs.topics.exclude` matches. A glob is an
  `fnmatch` pattern, matched case-sensitively against the path relative to the
  wiki root, and its `*` matches a `/` too.

`houseplants/snake-plant.md` is a topic. `houseplants.md` is not, because the
folder `houseplants/` is beside it.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from joshua_shared import frontmatter, layout

HOME_PAGE = "Home.md"
# The top-level wiki folders that hold no topic. Each has its own writer or
# its own purpose.
RESERVED_FOLDERS = (
    "journal",
    "people",
    "joshua-docs",
    "skills",
    layout.KNOWLEDGE,
    layout.INSIGHTS,
    layout.ATTACHMENTS,
)


@dataclass(frozen=True)
class Topic:
    """One topic page.

    `path` is relative to the wiki root (`houseplants/snake-plant.md`).
    `section` is its first path segment, or `""` for a page at the wiki root.
    `title` is the first `# ` heading of the body, else the frontmatter
    `title`, else the file stem.
    """

    path: str
    section: str
    title: str


def _excluded(rel_path: str, exclude: Sequence[str]) -> bool:
    return any(fnmatchcase(rel_path, pattern) for pattern in exclude)


def _shape_ok(rel_path: str) -> bool:
    """True when the path, by its text alone, can name a topic page."""
    if not rel_path.endswith(".md") or rel_path.startswith("/") or "\\" in rel_path:
        return False
    parts = rel_path.split("/")
    if any(not part or part.startswith(".") for part in parts):
        return False
    if rel_path == HOME_PAGE:
        return False
    return not (len(parts) > 1 and parts[0] in RESERVED_FOLDERS)


def is_topic_page(rel_path: str, wiki_root: Path, exclude: Sequence[str]) -> bool:
    """True when `rel_path`, relative to `wiki_root`, is a topic page.

    The page must be a file on the disk. See the module docstring for the rules.
    """
    if not _shape_ok(rel_path):
        return False
    absolute = wiki_root / rel_path
    if layout.is_hidden(absolute, wiki_root) or not absolute.is_file():
        return False
    if absolute.with_suffix("").is_dir():
        return False
    return not _excluded(rel_path, exclude)


def page_title(text: str, stem: str) -> str:
    """The title of a page: its first `# ` heading, its frontmatter `title`, or `stem`."""
    meta, body = frontmatter.split(text)
    for line in body.splitlines():
        if line.startswith("# "):
            heading = line[2:].strip()
            if heading:
                return heading
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return stem


def read_page(path: Path) -> str:
    """The text of a page, or `""` when it cannot be read as UTF-8."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def load_topic(rel_path: str, wiki_root: Path) -> Topic:
    """The `Topic` for a page, read from the disk. It does not check the rules."""
    pure = PurePosixPath(rel_path)
    section = pure.parts[0] if len(pure.parts) > 1 else ""
    title = page_title(read_page(wiki_root / rel_path), pure.stem)
    return Topic(path=rel_path, section=section, title=title)


def list_topics(wiki_root: Path, exclude: Sequence[str]) -> list[Topic]:
    """Every topic page under `wiki_root`, sorted by path."""
    wiki_root = Path(wiki_root)
    if not wiki_root.is_dir():
        return []
    found: list[str] = []
    for current, dirs, files in os.walk(wiki_root):
        here = Path(current)
        rel_dir = here.relative_to(wiki_root).as_posix()
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".") and not (rel_dir == "." and d in RESERVED_FOLDERS)
        ]
        for name in files:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if is_topic_page(rel, wiki_root, exclude):
                found.append(rel)
    return [load_topic(rel, wiki_root) for rel in sorted(found)]
