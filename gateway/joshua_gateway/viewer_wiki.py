"""Navigation over the wiki: titles, the folder tree, breadcrumbs, folder pages.

The wiki is a folder of Markdown files, and a folder can have an index page
beside it: ``recipes.md`` next to ``recipes/`` is the page for that folder.
``Home.md`` is the index page of the root. This module reads that shape once
per request and builds the pieces the viewer shows: a page title, the tree on
the home page, the crumbs above a page, the folder page, and the list of the
other pages in the same folder.

Everything here reads. The routes and the auth live in ``viewer``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from joshua_shared.layout import is_hidden

from joshua_gateway.viewer_journal import split_frontmatter

_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")

# The journal folder is a feed, not a tree: the home page links ``/journal``
# in its place.
JOURNAL_DIR = "journal"
# The shipped docs are closed on the tree, because a person did not write them.
CLOSED_DIRS = frozenset({"joshua-docs"})
# A folder with more pages than this, counting its subfolders, starts closed.
OPEN_MAX_PAGES = 12
# The other-pages list on a page shows at most this many siblings.
SIBLINGS_MAX = 40


@dataclass(frozen=True)
class Page:
    rel: str  # wiki-relative posix path
    title: str

    @property
    def url(self) -> str:
        return "/wiki/" + quote(self.rel)


@dataclass
class Folder:
    rel: str  # wiki-relative posix path, "" for the root
    name: str
    index: Page | None = None
    pages: list[Page] = field(default_factory=list)
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
        return len(self.pages) + sum(f.count() + (1 if f.index else 0) for f in self.folders)


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
    files = [e for e in entries if e.is_file() and e.suffix == ".md" and not is_hidden(e, wiki)]
    for file in files:
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


def tree_html(root: Folder, *, journal_pages: int | None = None) -> str:
    """The tree for the home page: the front page, the journal as one link,
    each folder as a collapsible block, then the loose pages of the root."""
    items: list[str] = []
    if root.index is not None:
        items.append(
            f'<li><a href="{root.index.url}">{escape(root.index.title)}</a>'
            " <span class=count>the front page</span></li>"
        )
    for folder in root.folders:
        if folder.name == JOURNAL_DIR:
            count = folder.count() if journal_pages is None else journal_pages
            items.append(
                f'<li><a href="/journal">journal</a> <span class=count>{count} pages</span></li>'
            )
            continue
        items.append(_folder_item(folder, depth=0))
    if root.pages:
        pages = "".join(_page_item(p) for p in root.pages)
        items.append(
            f"<li><details><summary>other pages <span class=count>{len(root.pages)}</span>"
            f"</summary><ul>{pages}</ul></details></li>"
        )
    return f'<ul class="tree">{"".join(items)}</ul>'


def _page_item(page: Page) -> str:
    return f'<li><a href="{page.url}">{escape(page.title)}</a></li>'


def _folder_item(folder: Folder, *, depth: int) -> str:
    count = folder.count()
    is_open = depth == 0 and folder.name not in CLOSED_DIRS and count <= OPEN_MAX_PAGES
    inner = "".join(_folder_item(f, depth=depth + 1) for f in folder.folders)
    inner += "".join(_page_item(p) for p in folder.pages)
    return (
        f"<li><details{' open' if is_open else ''}><summary>"
        f'<a href="{folder.url}">{escape(folder.title)}</a> <span class=count>{count}</span>'
        f"</summary><ul>{inner}</ul></details></li>"
    )


# -- crumbs and siblings ----------------------------------------------------


def crumbs_html(wiki: Path, rel: str, *, leaf: str | None) -> str:
    """``home › wiki › Recipes › Ponzu Sauce``. ``rel`` is the folder of the
    page (or the folder page itself); ``leaf`` is the page title, or None on
    a folder page where the folder is the last crumb."""
    parts = ['<a href="/">home</a>', '<a href="/wiki/">wiki</a>']
    segments = [s for s in rel.split("/") if s]
    for index, segment in enumerate(segments):
        folder_rel = "/".join(segments[: index + 1])
        index_file = wiki / f"{folder_rel}.md"
        text = _read(index_file) if index_file.is_file() else None
        title = page_title(text, stem_title(segment)) if text else stem_title(segment)
        if leaf is None and index == len(segments) - 1:
            parts.append(f"<span>{escape(title)}</span>")
        else:
            parts.append(f'<a href="/wiki/{quote(folder_rel)}/">{escape(title)}</a>')
    if leaf is not None:
        parts.append(f"<span>{escape(leaf)}</span>")
    return '<p class="crumbs">' + " › ".join(parts) + "</p>"


def siblings_html(wiki: Path, rel: str) -> str:
    """The other pages in the folder of the page at ``rel``, and the folders
    beside it, as one list at the foot of the page. Empty when there are none."""
    folder_rel = str(Path(rel).parent.as_posix()) if "/" in rel else ""
    if folder_rel == ".":
        folder_rel = ""
    folder = build_tree(wiki, folder_rel)
    if folder is None:
        return ""
    items = [
        f'<li><a href="{f.url}">{escape(f.title)}/</a></li>'
        for f in folder.folders
        if f.name != JOURNAL_DIR
    ]
    items += [_page_item(p) for p in folder.pages if p.rel != rel]
    if folder.index is not None and folder.index.rel != rel and folder.rel:
        items.insert(0, f'<li><a href="{folder.index.url}">{escape(folder.index.title)}</a></li>')
    if not items:
        return ""
    items = items[:SIBLINGS_MAX]
    return (
        f'<section class="siblings"><h2>more in {escape(folder.title)}</h2>'
        f'<ul class="tree">{"".join(items)}</ul></section>'
    )


# -- the folder page --------------------------------------------------------


def folder_html(folder: Folder, index_body_html: str) -> str:
    """A folder page: the title, the index page's text if it has one, then the
    subfolders and the pages."""
    parts = [f"<h1>{escape(folder.title)}</h1>"]
    if index_body_html:
        parts.append(f'<article class="md">{index_body_html}</article>')
    subfolders = "".join(
        f'<li><a href="{f.url}">{escape(f.title)}/</a> <span class=count>{f.count()}</span></li>'
        for f in folder.folders
        if f.name != JOURNAL_DIR or folder.rel
    )
    if any(f.name == JOURNAL_DIR for f in folder.folders) and not folder.rel:
        subfolders = '<li><a href="/journal">journal</a></li>' + subfolders
    pages = "".join(_page_item(p) for p in folder.pages)
    if subfolders:
        parts.append(f'<h2>folders</h2><ul class="tree">{subfolders}</ul>')
    if pages:
        parts.append(f'<h2>pages</h2><ul class="tree">{pages}</ul>')
    if not subfolders and not pages:
        parts.append("<p class=snippet>nothing here yet.</p>")
    return "".join(parts)


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


def meta_html(front: dict[str, Any]) -> str:
    """One muted line of the frontmatter, ``key: value`` pairs. A key with an
    empty value is left out. Empty frontmatter gives an empty string."""
    pairs = []
    for key, value in front.items():
        if value in (None, "", [], {}):
            continue
        pairs.append(f"<span class=k>{escape(str(key))}:</span> {escape(_short(value))}")
    if not pairs:
        return ""
    return '<p class="frontmatter meta">' + " · ".join(pairs) + "</p>"
