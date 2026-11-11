"""Navigation over the wiki: titles, the folder tree, crumbs, siblings.

The wiki is a folder of Markdown files, and a folder can have an index page
beside it: ``recipes.md`` next to ``recipes/`` is the page for that folder.
``Home.md`` is the index page of the root. This module reads that shape once
per request and builds the pieces the viewer shows: a page title, the folder
tree (``build_tree``, which the JSON routes answer from), the crumbs above a
page, the other pages in the same folder, and the frontmatter line of a page.

Everything here reads, and every answer is a plain value: the JSON routes of
``viewer_api`` send it, and the React app draws it. The routes and the auth
live in ``viewer``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from joshua_shared.attachments import is_meta
from joshua_shared.layout import is_hidden

from joshua_gateway.viewer_journal import split_frontmatter

_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")

# The journal folder is a feed, not a tree: the home page links ``/journal``
# in its place.
JOURNAL_DIR = "journal"
# The other-pages list on a page shows at most this many siblings.
SIBLINGS_MAX = 40


@dataclass(frozen=True)
class Page:
    rel: str  # wiki-relative posix path
    title: str

    @property
    def url(self) -> str:
        return "/wiki/" + quote(self.rel)


@dataclass(frozen=True)
class File:
    """A file in the wiki that is not a page: a picture, a PDF, a text file."""

    rel: str  # wiki-relative posix path
    name: str
    size: int

    @property
    def url(self) -> str:
        return "/wiki/" + quote(self.rel)


@dataclass
class Folder:
    rel: str  # wiki-relative posix path, "" for the root
    name: str
    index: Page | None = None
    pages: list[Page] = field(default_factory=list)
    files: list[File] = field(default_factory=list)
    folders: list[Folder] = field(default_factory=list)

    @property
    def title(self) -> str:
        if self.index is not None:
            return self.index.title
        return stem_title(self.name) if self.name else "wiki"

    @property
    def url(self) -> str:
        return "/wiki/" + (quote(self.rel) + "/" if self.rel else "")

    def count(self) -> int:
        return (
            len(self.pages)
            + len(self.files)
            + sum(f.count() + (1 if f.index else 0) for f in self.folders)
        )


# -- titles -----------------------------------------------------------------


def stem_title(stem: str) -> str:
    return stem.replace("-", " ").replace("_", " ")


def first_heading(body: str) -> tuple[str, str]:
    """``(heading, body without that line)`` for the first ``# `` line before
    any other text, or ``("", body)``."""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        match = _H1.match(stripped)
        if match:
            rest = "\n".join(lines[:index] + lines[index + 1 :]).lstrip("\n")
            return match.group(1).strip(), rest
        break
    return "", body


def page_title(text: str, fallback: str) -> str:
    """The frontmatter ``title``, else the first ``# `` heading, else ``fallback``."""
    front, body = split_frontmatter(text)
    title = front.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    heading, _ = first_heading(body)
    return heading or fallback


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _page(wiki: Path, file: Path) -> Page:
    text = _read(file) or ""
    return Page(file.relative_to(wiki).as_posix(), page_title(text, stem_title(file.stem)))


def _file(wiki: Path, file: Path) -> File:
    try:
        size = file.stat().st_size
    except OSError:
        size = 0
    return File(file.relative_to(wiki).as_posix(), file.name, size)


# -- the tree ---------------------------------------------------------------


def build_tree(wiki: Path, rel: str = "") -> Folder | None:
    """The folder at ``rel`` (``""`` for the whole wiki) with its pages, its
    subfolders, and each subfolder's index page. None when it is not a folder
    or names a hidden entry. A page beside a folder of the same name is that
    folder's index and is not listed among the pages."""
    directory = wiki / rel if rel else wiki
    if not directory.is_dir() or (rel and is_hidden(directory, wiki)):
        return None
    folder = Folder(rel=rel, name=Path(rel).name if rel else "")
    parent = directory.parent if rel else None
    if rel and parent is not None:
        index_file = parent / f"{folder.name}.md"
        if index_file.is_file():
            folder.index = _page(wiki, index_file)
    elif (wiki / "Home.md").is_file():
        folder.index = _page(wiki, wiki / "Home.md")
    _fill(wiki, directory, folder)
    return folder


def _fill(wiki: Path, directory: Path, folder: Folder) -> None:
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return
    subdirs = [e for e in entries if e.is_dir() and not is_hidden(e, wiki)]
    subdir_names = {d.name for d in subdirs}
    visible = [e for e in entries if e.is_file() and not is_hidden(e, wiki)]
    for file in visible:
        if file.suffix != ".md":
            # A wiki attachment: the picture a page shows, or a document. It is
            # not a page, so it is listed apart from them and never titled.
            if not is_meta(file):
                folder.files.append(_file(wiki, file))
            continue
        if file.stem in subdir_names:
            continue  # the index page of a subfolder; it is listed with the folder
        if folder.rel == "" and file.name == "Home.md":
            continue  # the root's index page
        folder.pages.append(_page(wiki, file))
    for sub in subdirs:
        child = Folder(rel=sub.relative_to(wiki).as_posix(), name=sub.name)
        index_file = directory / f"{sub.name}.md"
        if index_file.is_file():
            child.index = _page(wiki, index_file)
        _fill(wiki, sub, child)
        folder.folders.append(child)


# -- crumbs and siblings ----------------------------------------------------


def crumbs(wiki: Path, rel: str, *, leaf: str | None) -> list[dict[str, str]]:
    """``home › wiki › Recipes › Ponzu Sauce`` as ``{"title", "url"}`` values.

    ``rel`` is the folder of the page (or the folder page itself); ``leaf`` is
    the page title, or None on a folder page where the folder is the last
    crumb. The last crumb is where the reader is, so its ``url`` is empty and
    the app draws it as text.
    """
    items = [{"title": "home", "url": "/"}, {"title": "wiki", "url": "/wiki/"}]
    segments = [s for s in rel.split("/") if s]
    for index, segment in enumerate(segments):
        folder_rel = "/".join(segments[: index + 1])
        index_file = wiki / f"{folder_rel}.md"
        text = _read(index_file) if index_file.is_file() else None
        title = page_title(text, stem_title(segment)) if text else stem_title(segment)
        here = leaf is None and index == len(segments) - 1
        items.append({"title": title, "url": "" if here else f"/wiki/{quote(folder_rel)}/"})
    if leaf is not None:
        items.append({"title": leaf, "url": ""})
    return items


def siblings(wiki: Path, rel: str) -> list[dict[str, str]]:
    """The other pages in the folder of the page at ``rel``, and the folders
    beside it, as one list of ``{"title", "url"}`` values. The title of a
    folder ends with ``/``, because the list holds both kinds. Empty when
    there is no sibling."""
    folder_rel = str(Path(rel).parent.as_posix()) if "/" in rel else ""
    if folder_rel == ".":
        folder_rel = ""
    folder = build_tree(wiki, folder_rel)
    if folder is None:
        return []
    items = [
        {"title": f"{f.title}/", "url": f.url} for f in folder.folders if f.name != JOURNAL_DIR
    ]
    items += [{"title": p.title, "url": p.url} for p in folder.pages if p.rel != rel]
    if folder.index is not None and folder.index.rel != rel and folder.rel:
        items.insert(0, {"title": folder.index.title, "url": folder.index.url})
    return items[:SIBLINGS_MAX]


# -- the frontmatter line ---------------------------------------------------


def _short(value: Any) -> str:
    """A value for the meta line: a timestamp keeps its day, a list joins,
    anything else is its text."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    text = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T[0-9:.]+Z?", text):
        return text[:10]
    return text


def meta_pairs(front: dict[str, Any]) -> dict[str, str]:
    """The frontmatter of a page as text, for the one muted line above it.

    Each value is the text a person reads: a timestamp keeps its day and a
    list joins. A key with an empty value is left out, so empty frontmatter
    gives an empty map. The order is the order of the page."""
    return {
        str(key): _short(value) for key, value in front.items() if value not in (None, "", [], {})
    }
