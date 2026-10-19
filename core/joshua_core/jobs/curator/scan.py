"""The scan: the free checks the curator runs over the whole wiki every run.

No model and no database. `load_tree` lists the wiki, `parse_links` and
`resolve_link` build the link graph, and `scan_tree` returns the findings of
the checks:

- `broken-link`: a link to a page or a file that is not there. Tier A when
  exactly one page has the file name the link names, else Tier C.
- `orphan`: a page that `Home.md` does not reach over links. Tier A when its
  folder has an index page, else Tier C.
- `index-line`: a list line of an index page whose links all go nowhere.
  Tier A.
- `stray`: `._*`, `.DS_Store`, an empty `.md`, or `REPORT.md` or
  `PRUNE.txt` at the root. Tier A, or Tier C when `trash` is off.
- `journal-date`: a journal entry with no `date` in its frontmatter. Tier A.

A link is `[text](target)` (not an image) or `[[target]]`. A Markdown target
is relative to the folder of the page, or to the wiki root when it starts
with `/wiki/` or `/`, with or without `.md`. A wikilink is tried relative to
the folder of the page, then to the wiki root, then by the file name of a page
anywhere in the wiki when exactly one page has it. A URL with a scheme
(`https:`, `mailto:`), a target that is only an anchor, and a target that
leaves the wiki are not checked. A link into `attachments/` is checked for the
file only. Links inside code and inside the frontmatter do not count. A fenced
code block opens with three or more backticks or tildes, at most three spaces
in, and closes at a line of the same mark that is at least as long and holds
nothing else, as in CommonMark.

The scan does not read the links of a page under `people/` or `joshua-docs/`,
and it never proposes a change there. A journal entry is read for links, but a
broken link in it is Tier C, because the curator never changes a journal body.
"""

from __future__ import annotations

import os
import posixpath
import re
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from functools import cached_property
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote

from joshua_shared import frontmatter, layout

from joshua_core.jobs.curator.findings import TIER_A, TIER_C, Finding
from joshua_core.jobs.curator.track import UNTOUCHED_FOLDERS, body_hash, is_job_report
from joshua_core.jobs.topics import page_title

HOME_PAGE = "Home.md"
JOURNAL = "journal"
ATTACHMENTS = layout.ATTACHMENTS
# A page in these folders is never an orphan, and its links never make
# another page reachable from `Home.md`.
OUT_OF_SCOPE = (
    JOURNAL,
    "people",
    "joshua-docs",
    layout.KNOWLEDGE,
    layout.INSIGHTS,
    "skills",
    ATTACHMENTS,
)
STRAY_NAMES = (".DS_Store",)
STRAY_PREFIX = "._"
ROOT_STRAYS = ("REPORT.md", "PRUNE.txt")
PAGES_HEADING = "## Pages"
SECTIONS_HEADING = "sections"
SUMMARY_CHARS = 400

_MD_LINK_RE = re.compile(
    r"(?<!!)\[(?P<text>[^\]\n]*)\]\((?P<dest><[^>\n]*>|[^)\s]+)(?:\s+\"[^\"\n]*\")?\)"
)
_WIKILINK_RE = re.compile(
    r"\[\[(?P<target>[^\]|#\n]+)(?P<anchor>#[^\]|\n]*)?(?P<label>\|[^\]\n]*)?\]\]"
)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
HEADING_RE = re.compile(r"^(#{1,6})\s")
# Characters that would end a Markdown link target that is not in `<...>`.
_UNSAFE_TARGET_CHARS = set(" ()<>\t")


# -- the tree ---------------------------------------------------------------


def read_exact(path: Path) -> str:
    """The text of a file, with its line ends as they are, or `""` when it
    cannot be read as UTF-8."""
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def section_of(rel: str) -> str:
    """The first folder of a wiki path, or `""` for a page at the root."""
    parts = PurePosixPath(rel).parts
    return parts[0] if len(parts) > 1 else ""


def _is_stray_name(rel: str) -> bool:
    name = PurePosixPath(rel).name
    if name in STRAY_NAMES or name.startswith(STRAY_PREFIX):
        return True
    return rel in ROOT_STRAYS


@dataclass(frozen=True)
class Tree:
    """The files of the wiki at one moment, as paths relative to the wiki root.

    `files` holds every file that is not on a dot path. `strays` holds the
    stray files, with the dot files among them. `empty` holds the `.md` files
    with no text but whitespace.
    """

    root: Path
    files: frozenset[str]
    strays: frozenset[str] = frozenset()
    empty: frozenset[str] = frozenset()

    @cached_property
    def pages(self) -> frozenset[str]:
        """Every `.md` file that is not a stray."""
        return frozenset(f for f in self.files if f.endswith(".md") and f not in self.strays)

    @cached_property
    def dirs(self) -> frozenset[str]:
        out: set[str] = set()
        for rel in self.files:
            parent = PurePosixPath(rel).parent
            while str(parent) not in (".", ""):
                out.add(parent.as_posix())
                parent = parent.parent
        return frozenset(out)

    @cached_property
    def by_basename(self) -> dict[str, tuple[str, ...]]:
        found: dict[str, list[str]] = {}
        for rel in sorted(self.pages):
            found.setdefault(PurePosixPath(rel).name, []).append(rel)
        return {name: tuple(paths) for name, paths in found.items()}

    def text(self, rel: str) -> str:
        """The text of a file, with its line ends as they are on the disk."""
        return read_exact(self.root / rel)

    def is_index_page(self, rel: str) -> bool:
        """True for `Home.md` and for a page `X.md` with a folder `X/` beside it."""
        return rel == HOME_PAGE or (rel.endswith(".md") and rel[:-3] in self.dirs)

    def index_of_folder(self, folder: str) -> str | None:
        """The index page of a folder: `X.md` for `X/`, or `Home.md` for the root."""
        rel = HOME_PAGE if folder in ("", ".") else f"{folder}.md"
        return rel if rel in self.pages else None

    def renamed(self, moves: dict[str, str | None]) -> Tree:
        """The tree after each `old -> new` move, where None removes the file."""
        files = set(self.files)
        for old, new in moves.items():
            files.discard(old)
            if new is not None:
                files.add(new)
        return Tree(self.root, frozenset(files), self.strays - set(moves), self.empty)


def _blank(path: Path) -> bool:
    try:
        if path.stat().st_size == 0:
            return True
        if path.stat().st_size > 4096:
            return False
        return not path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False


def load_tree(wiki_root: Path) -> Tree:
    """List the wiki. A dot folder (`.git`, `.trash`) is not entered."""
    wiki_root = Path(wiki_root)
    files: set[str] = set()
    strays: set[str] = set()
    empty: set[str] = set()
    if not wiki_root.is_dir():
        return Tree(wiki_root, frozenset())
    for current, dirs, names in os.walk(wiki_root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        here = Path(current)
        rel_dir = here.relative_to(wiki_root).as_posix()
        for name in names:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            path = here / name
            if not path.is_file():
                continue
            if _is_stray_name(rel):
                strays.add(rel)
                if not name.startswith("."):
                    files.add(rel)
                continue
            if name.startswith("."):
                continue
            files.add(rel)
            if name.endswith(".md") and _blank(path):
                empty.add(rel)
                if section_of(rel) != JOURNAL and rel != HOME_PAGE:
                    strays.add(rel)
    return Tree(wiki_root, frozenset(files), frozenset(strays), frozenset(empty))


# -- links ------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    """One link in a page.

    `start` and `end` are the offsets of the whole link in the page text, and
    `target_start` and `target_end` the offsets of its target. `target` is the
    target as written, with `<` and `>` for a Markdown target in brackets.
    """

    raw: str
    target: str
    wikilink: bool
    start: int
    end: int
    target_start: int
    target_end: int
    text: str = ""


def _mask(text: str) -> str:
    """`text` with the frontmatter, the code blocks, and the code spans blanked.

    Each blanked character becomes a space and each line end stays, so an
    offset into the mask is an offset into the text.
    """
    chars = list(text)
    meta_end = len(text) - len(frontmatter.split(text)[1])
    for i in range(meta_end):
        if chars[i] != "\n":
            chars[i] = " "
    offset = 0
    fence = ""
    for line in text.splitlines(keepends=True):
        start = offset
        offset += len(line)
        if start < meta_end:
            continue
        match = _FENCE_RE.match(line)
        mark = match.group(1) if match else ""
        if fence:
            rest = line[match.end() :] if match else ""
            if mark[:1] == fence[:1] and len(mark) >= len(fence) and not rest.strip():
                fence = ""
        elif mark:
            fence = mark
        else:
            continue
        for i in range(start, offset):
            if chars[i] != "\n":
                chars[i] = " "
    masked = "".join(chars)
    return _INLINE_CODE_RE.sub(lambda m: " " * len(m.group()), masked)


def parse_links(text: str) -> list[Link]:
    """Every link in `text`, in order. See the module docstring."""
    masked = _mask(text)
    found: list[Link] = []
    for m in _MD_LINK_RE.finditer(masked):
        found.append(
            Link(
                raw=text[m.start() : m.end()],
                target=text[m.start("dest") : m.end("dest")],
                wikilink=False,
                start=m.start(),
                end=m.end(),
                target_start=m.start("dest"),
                target_end=m.end("dest"),
                text=text[m.start("text") : m.end("text")],
            )
        )
    for m in _WIKILINK_RE.finditer(masked):
        found.append(
            Link(
                raw=text[m.start() : m.end()],
                target=text[m.start("target") : m.end("target")],
                wikilink=True,
                start=m.start(),
                end=m.end(),
                target_start=m.start("target"),
                target_end=m.end("target"),
                text=(m.group("label") or "")[1:],
            )
        )
    found.sort(key=lambda link: link.start)
    return found


@dataclass(frozen=True)
class Resolution:
    """Where a link goes.

    `checked` is False for a link the scan does not check (a URL, an anchor, a
    path out of the wiki). `path` is the wiki path it reaches, or None when
    the target is not there. `wanted` is the path the target names, so a
    broken link can be matched by its file name. `attachment` is True for a
    target in `attachments/`.
    """

    checked: bool
    path: str | None = None
    wanted: str | None = None
    attachment: bool = False


_UNCHECKED = Resolution(checked=False)


def _normalize(parts: Iterable[str]) -> str | None:
    out: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not out:
                return None
            out.pop()
            continue
        out.append(part)
    return "/".join(out) if out else None


def _split_dest(dest: str) -> tuple[str, str]:
    """`(path, suffix)`: the path part of a Markdown target and its `#...` or `?...`."""
    inner = dest[1:-1] if dest.startswith("<") and dest.endswith(">") else dest
    cut = len(inner)
    for mark in ("#", "?"):
        index = inner.find(mark)
        if index != -1:
            cut = min(cut, index)
    return inner[:cut], inner[cut:]


def _folder(rel: str) -> list[str]:
    return list(PurePosixPath(rel).parent.parts)


def resolve_link(link: Link, source: str, tree: Tree) -> Resolution:
    """Where `link`, in the page `source`, goes in `tree`. See the module docstring."""
    if link.wikilink:
        raw = unquote(link.target.strip())
        if not raw or "\\" in raw:
            return _UNCHECKED
        name = raw if raw.endswith(".md") else f"{raw}.md"
        first: str | None = None
        for base in (_folder(source), []):
            rel = _normalize([*base, *name.split("/")])
            if rel is None:
                continue
            first = first or rel
            if rel in tree.pages:
                return Resolution(checked=True, path=rel, wanted=rel)
        matches = tree.by_basename.get(PurePosixPath(name).name, ())
        if len(matches) == 1:
            return Resolution(checked=True, path=matches[0], wanted=first)
        return Resolution(checked=True, path=None, wanted=first)

    path, _ = _split_dest(link.target)
    if not path or path.startswith("//") or _SCHEME_RE.match(path):
        return _UNCHECKED
    path = unquote(path)
    if "\\" in path:
        return _UNCHECKED
    if path.startswith("/wiki/"):
        parts, base = path[len("/wiki/") :].split("/"), []
    elif path.startswith("/"):
        parts, base = path[1:].split("/"), []
    else:
        parts, base = path.split("/"), _folder(source)
    rel = _normalize([*base, *parts])
    if rel is None:
        return _UNCHECKED
    if PurePosixPath(rel).parts[0] == ATTACHMENTS:
        return Resolution(
            checked=True, path=rel if rel in tree.files else None, wanted=rel, attachment=True
        )
    if rel in tree.files:
        return Resolution(checked=True, path=rel, wanted=rel)
    if not rel.endswith(".md") and f"{rel}.md" in tree.pages:
        return Resolution(checked=True, path=f"{rel}.md", wanted=f"{rel}.md")
    wanted = rel if PurePosixPath(rel).suffix else f"{rel}.md"
    return Resolution(checked=True, path=None, wanted=wanted)


def encode_target(path: str) -> str:
    """A wiki path as a Markdown link target: percent-encoded when it holds a
    space, a parenthesis, or an angle bracket."""
    if any(ch in _UNSAFE_TARGET_CHARS for ch in path):
        return quote(path, safe="/")
    return path


def target_for(link: Link, source: str, wanted: str) -> str:
    """The target text that makes `link`, in the page `source`, reach `wanted`.

    It keeps the form of the link: a wikilink stays a wikilink, with the path
    from the wiki root; a Markdown target keeps `/wiki/`, `/`, or a relative
    path, its `.md` or the lack of it, and its anchor.
    """
    if link.wikilink:
        return wanted[:-3] if wanted.endswith(".md") else wanted
    path, suffix = _split_dest(link.target)
    plain = unquote(path)
    new = wanted
    if not plain.endswith(".md") and new.endswith(".md") and not PurePosixPath(plain).suffix:
        new = new[:-3]
    if plain.startswith("/wiki/"):
        new = "/wiki/" + new
    elif plain.startswith("/"):
        new = "/" + new
    else:
        folder = PurePosixPath(source).parent.as_posix()
        new = posixpath.relpath(new, folder if folder != "." else ".")
    if link.target.startswith("<"):
        return f"<{new}{suffix}>"
    return encode_target(new) + suffix


def retarget(text: str, link: Link, new_target: str) -> str:
    """`text` with the target of `link` replaced by `new_target`."""
    return text[: link.target_start] + new_target + text[link.target_end :]


# -- the checks -------------------------------------------------------------


def _is_list_line(line: str) -> bool:
    return bool(LIST_LINE_RE.match(line))


def _line_at(text: str, offset: int) -> str:
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    return text[start : end if end != -1 else len(text)]


def link_sources(tree: Tree) -> list[str]:
    """The pages whose links the scan reads: every page outside `people/` and
    `joshua-docs/`."""
    return sorted(p for p in tree.pages if section_of(p) not in UNTOUCHED_FOLDERS)


def link_graph(tree: Tree) -> dict[str, list[tuple[Link, Resolution]]]:
    """`{page: [(link, where it goes), ...]}` for every page in `link_sources`."""
    graph: dict[str, list[tuple[Link, Resolution]]] = {}
    for page in link_sources(tree):
        text = tree.text(page)
        graph[page] = [(link, resolve_link(link, page, tree)) for link in parse_links(text)]
    return graph


def reachable(tree: Tree, graph: dict[str, list[tuple[Link, Resolution]]]) -> set[str]:
    """The pages that `Home.md` reaches over links. A page in `OUT_OF_SCOPE` is
    reached, but its own links are not followed."""
    if HOME_PAGE not in tree.pages:
        return set()
    seen = {HOME_PAGE}
    queue = deque([HOME_PAGE])
    while queue:
        page = queue.popleft()
        if section_of(page) in OUT_OF_SCOPE:
            continue
        for _link, where in graph.get(page, []):
            if where.path and where.path in tree.pages and where.path not in seen:
                seen.add(where.path)
                queue.append(where.path)
    return seen


def _hashes(tree: Tree, paths: Iterable[str]) -> dict[str, str]:
    return {p: body_hash(tree.text(p)) for p in paths if p in tree.pages}


def _broken_links(tree: Tree, graph: dict[str, list[tuple[Link, Resolution]]]) -> list[Finding]:
    found: list[Finding] = []
    for page, links in graph.items():
        text = tree.text(page)
        journal = section_of(page) == JOURNAL
        dead_lines: dict[str, list[Link]] = {}
        live_lines: set[str] = set()
        fixes: list[tuple[Link, str]] = []
        dead: list[tuple[Link, Resolution, tuple[str, ...]]] = []
        for link, where in links:
            line = _line_at(text, link.start)
            if not where.checked:
                continue
            if where.path is not None:
                live_lines.add(line)
                continue
            candidates: tuple[str, ...] = ()
            if not where.attachment and where.wanted and where.wanted.endswith(".md"):
                candidates = tree.by_basename.get(PurePosixPath(where.wanted).name, ())
            if len(candidates) == 1 and not link.wikilink:
                fixes.append((link, candidates[0]))
                live_lines.add(line)
                continue
            dead.append((link, where, candidates))
            dead_lines.setdefault(line, []).append(link)

        for link, candidate in fixes:
            new = link.raw[: link.target_start - link.start]
            new += target_for(link, page, candidate) + link.raw[link.target_end - link.start :]
            detail = {"target": link.target, "candidates": [candidate], "line": link.raw}
            if journal:
                found.append(Finding("broken-link", TIER_C, [page], link.raw, detail))
            else:
                patch = {"action": "relink", "path": page, "old": link.raw, "new": new}
                found.append(Finding("broken-link", TIER_A, [page], link.raw, detail, patch))

        dropped: set[str] = set()
        if tree.is_index_page(page):
            for line in dead_lines:
                if _is_list_line(line) and line not in live_lines and line.strip():
                    dropped.add(line)
                    patch = {"action": "drop-line", "path": page, "line": line}
                    detail = {"line": line, "targets": [ln.target for ln in dead_lines[line]]}
                    found.append(Finding("index-line", TIER_A, [page], line, detail, patch))
        for link, where, candidates in dead:
            if _line_at(text, link.start) in dropped:
                continue
            detail = {
                "target": link.target,
                "wanted": where.wanted,
                "candidates": list(candidates),
                "line": link.raw,
            }
            found.append(Finding("broken-link", TIER_C, [page], link.raw, detail))
    return found


def _orphans(tree: Tree, graph: dict[str, list[tuple[Link, Resolution]]]) -> list[Finding]:
    seen = reachable(tree, graph)
    found: list[Finding] = []
    for page in sorted(tree.pages):
        if page == HOME_PAGE or page in seen or section_of(page) in OUT_OF_SCOPE:
            continue
        folder = PurePosixPath(page).parent.as_posix()
        index = tree.index_of_folder(folder)
        title = page_title(tree.text(page), PurePosixPath(page).stem)
        detail = {"title": title, "index": index}
        if index is None or index == page:
            found.append(Finding("orphan", TIER_C, [page], "", detail))
            continue
        line = f"- [{title}](/wiki/{encode_target(page)})"
        patch = {"action": "index-add", "path": index, "page": page, "line": line}
        found.append(Finding("orphan", TIER_A, [page, index], "", detail, patch))
    return found


def _strays(tree: Tree, trash: bool) -> list[Finding]:
    found: list[Finding] = []
    for rel in sorted(tree.strays):
        detail = {"empty": rel in tree.empty}
        if trash and section_of(rel) not in UNTOUCHED_FOLDERS:
            patch = {"action": "trash", "path": rel}
            found.append(Finding("stray", TIER_A, [rel], "", detail, patch))
        else:
            found.append(Finding("stray", TIER_C, [rel], "", detail))
    return found


def _journal_dates(tree: Tree) -> list[Finding]:
    data_root = tree.root.parent
    found: list[Finding] = []
    for rel in sorted(tree.pages):
        if section_of(rel) != JOURNAL:
            continue
        day = layout.journal_day_from_path(tree.root / rel, data_root)
        if day is None:
            continue
        text = tree.text(rel)
        meta, body = frontmatter.split(text)
        if body == text and text.startswith("---"):
            # A block that does not close or is not a mapping: leave it to a person.
            continue
        if "date" in meta:
            continue
        patch = {"action": "set-date", "path": rel, "date": day.isoformat()}
        found.append(Finding("journal-date", TIER_A, [rel], day.isoformat(), {}, patch))
    return found


@dataclass
class Scan:
    """What `scan_tree` found."""

    tree: Tree
    graph: dict[str, list[tuple[Link, Resolution]]]
    findings: list[Finding] = field(default_factory=list)


def scan_tree(tree: Tree, *, trash: bool) -> Scan:
    """Run every check of the module docstring over `tree`."""
    graph = link_graph(tree)
    findings = [
        *_strays(tree, trash),
        *_journal_dates(tree),
        *_broken_links(tree, graph),
        *_orphans(tree, graph),
    ]
    for finding in findings:
        finding.detail.setdefault("hashes", _hashes(tree, finding.paths))
    return Scan(tree=tree, graph=graph, findings=findings)


def is_journal_report(tree: Tree, rel: str) -> bool:
    """True for a journal entry that a job run wrote."""
    if section_of(rel) != JOURNAL:
        return False
    meta, _ = frontmatter.split(tree.text(rel))
    return is_job_report(meta)


# -- the section map --------------------------------------------------------


@dataclass(frozen=True)
class Section:
    """One section of `Home.md`: its link text, its index page, its folder,
    the text after the link, and the first paragraph of its index page."""

    name: str
    path: str
    folder: str
    description: str
    summary: str


def _first_paragraph(text: str) -> str:
    _, body = frontmatter.split(text)
    lines: list[str] = []
    for line in body.splitlines():
        if not line.strip():
            if lines:
                break
            continue
        if HEADING_RE.match(line):
            if lines:
                break
            continue
        lines.append(line.strip())
    return " ".join(lines)[:SUMMARY_CHARS]


def section_map(tree: Tree) -> list[Section]:
    """The `## Sections` list of `Home.md`, one `Section` for each list line
    with a link to a page."""
    if HOME_PAGE not in tree.pages:
        return []
    text = tree.text(HOME_PAGE)
    _, body = frontmatter.split(text)
    inside = False
    out: list[Section] = []
    for line in body.splitlines():
        heading = HEADING_RE.match(line)
        if heading:
            inside = len(heading.group(1)) == 2 and line[3:].strip().lower() == SECTIONS_HEADING
            continue
        if not inside or not _is_list_line(line):
            continue
        links = parse_links(line)
        if not links:
            continue
        link = links[0]
        where = resolve_link(link, HOME_PAGE, tree)
        if where.path is None or not where.path.endswith(".md"):
            continue
        stem = where.path[:-3]
        folder = stem if stem in tree.dirs else PurePosixPath(where.path).parent.as_posix()
        rest = line[link.end :].strip().lstrip(":-–—").strip()
        out.append(
            Section(
                name=(link.text or link.target).strip(),
                path=where.path,
                folder="" if folder == "." else folder,
                description=rest,
                summary=_first_paragraph(tree.text(where.path)),
            )
        )
    return out


def render_section_map(sections: list[Section]) -> str:
    """The section map as the text the `place` worker reads."""
    lines: list[str] = []
    for s in sections:
        lines.append(f"- section: {s.name}")
        lines.append(f"  folder: {s.folder or '(wiki root)'}")
        if s.description:
            lines.append(f"  description: {s.description}")
        if s.summary:
            lines.append(f"  index page: {s.summary}")
    return "\n".join(lines)


# -- near-duplicates --------------------------------------------------------


@dataclass(frozen=True)
class Pair:
    """Two topic pages to compare. `kind` is `duplicate` at or above
    `duplicate_sim`, or `overlap` for two pages of one section at or above
    `overlap_sim`."""

    a: str
    b: str
    sim: float
    kind: str


def _unit(vector: list[float]) -> list[float] | None:
    norm = sum(x * x for x in vector) ** 0.5
    if norm == 0:
        return None
    return [x / norm for x in vector]


def similar_pairs(
    vectors: dict[str, list[float]], *, duplicate_sim: float, overlap_sim: float
) -> list[Pair]:
    """Every pair of pages at or above the floors, the closest first.

    `vectors` maps a wiki path to the mean vector of the page.
    """
    units = {p: u for p, v in vectors.items() if (u := _unit(v)) is not None}
    paths = sorted(units)
    pairs: list[Pair] = []
    for i, a in enumerate(paths):
        ua = units[a]
        for b in paths[i + 1 :]:
            sim = sum(x * y for x, y in zip(ua, units[b], strict=False))
            if sim >= duplicate_sim:
                pairs.append(Pair(a, b, sim, "duplicate"))
            elif sim >= overlap_sim and section_of(a) == section_of(b):
                pairs.append(Pair(a, b, sim, "overlap"))
    pairs.sort(key=lambda p: (p.kind != "duplicate", -p.sim, p.a, p.b))
    return pairs


def journal_day(tree: Tree, rel: str) -> date | None:
    """The day of a journal path, from its folders."""
    return layout.journal_day_from_path(tree.root / rel, tree.root.parent)
