"""The apply step: the one place where the curator changes the wiki.

`Workspace` holds the writes of one run, or of one chat apply, and refuses a
write that the curator must never make:

- a path under `people/` or `joshua-docs/`, or a dot path, except the
  destination of a file it moves to `wiki/.trash/<day>/`;
- a change to the body of a journal entry, and a move of a journal entry;
- a change to the frontmatter of a skill page other than `curated` and
  `curated_hash`.

`apply_patch` applies one finding's patch. Each action checks again, on the
disk, that the patch still fits, and raises `ApplyRefused` when it does not:

- `relink`: replace a link that goes nowhere with one that goes to the page.
- `drop-line`: remove a list line of an index page.
- `index-add`: add a list line for a page under `## Pages` on an index page.
- `trash`: move a file to `wiki/.trash/<day>/<path>`.
- `set-date`: set `date` in the frontmatter of a journal entry.
- `move`: rename a page into another folder, with the same file name. Every
  link to it, and every relative link in it, is rewritten; it leaves the index
  page of its old folder and joins the index page of its new one.
- `merge`: point every link to a page at its duplicate, then move the page to
  the trash. No stub stays.
- `refresh`: apply find and replace pairs in order to the body of a page.
  Each `find` must occur exactly once when its turn comes, or nothing changes.

A link in a journal entry is never rewritten, because the curator never
changes a journal body. The next scan reports a link that a move broke there.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from joshua_shared import frontmatter, layout

from joshua_core.jobs.curator import scan
from joshua_core.jobs.curator.scan import Tree, load_tree, parse_links, resolve_link, target_for
from joshua_core.jobs.curator.track import (
    CURATED_KEY,
    HASH_KEY,
    is_untouched,
    may_track,
    needs_review,
    with_tracking,
)

SKILLS = "skills"
ACTIONS = (
    "relink",
    "drop-line",
    "index-add",
    "trash",
    "set-date",
    "move",
    "merge",
    "refresh",
)


class ApplyRefused(Exception):
    """A patch does not fit the wiki as it is now, or would break a rule."""


def _body(text: str) -> tuple[str, str]:
    """`(head, body)`: the frontmatter block with its fences, and the rest."""
    _, body = frontmatter.split(text)
    return text[: len(text) - len(body)], body


def _without_tracking(meta: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in meta.items() if k not in (CURATED_KEY, HASH_KEY)}


class Workspace:
    """The writes of one run. Every write goes to the disk at once.

    `written` lists each wiki path that changed, in order: a page, the source
    and the destination of a move, and a file in the trash. `clean` holds the
    current path of each page that had current tracking keys before its first
    change, so the caller can give it new keys after the change.
    """

    def __init__(self, wiki_root: Path, day: date) -> None:
        self.root = Path(wiki_root)
        self.day = day
        self.written: list[str] = []
        self.clean: set[str] = set()
        self.moves: dict[str, str | None] = {}
        self._seen: set[str] = set()

    # -- bookkeeping --------------------------------------------------------

    def _mark(self, *rels: str) -> None:
        for rel in rels:
            if rel not in self.written:
                self.written.append(rel)

    def _note_clean(self, rel: str, text: str) -> None:
        if rel in self._seen:
            return
        self._seen.add(rel)
        if rel.endswith(".md") and not needs_review(text):
            self.clean.add(rel)

    def current(self, rel: str) -> str | None:
        """Where a path of the start of the run is now, or None when it is in the trash."""
        seen: set[str] = set()
        while rel in self.moves and rel not in seen:
            seen.add(rel)
            nxt = self.moves[rel]
            if nxt is None:
                return None
            rel = nxt
        return rel

    def paths(self) -> list[Path]:
        """`written`, as absolute paths."""
        return [self.root / rel for rel in self.written]

    # -- reads and writes ---------------------------------------------------

    def read(self, rel: str) -> str:
        return scan.read_exact(self.root / rel)

    def write(self, rel: str, text: str) -> bool:
        """Write a page. Returns False when the text did not change."""
        if is_untouched(rel):
            raise ApplyRefused(f"the curator never writes {rel}")
        path = self.root / rel
        old = self.read(rel) if path.is_file() else ""
        if old == text:
            return False
        section = scan.section_of(rel)
        if section == scan.JOURNAL and path.is_file() and _body(old)[1] != _body(text)[1]:
            raise ApplyRefused(f"the curator never changes the body of a journal entry: {rel}")
        if section == SKILLS and path.is_file():
            before, _ = frontmatter.split(old)
            after, _ = frontmatter.split(text)
            if _without_tracking(before) != _without_tracking(after):
                raise ApplyRefused(f"the curator adds only its tracking keys to a skill: {rel}")
        self._note_clean(rel, old)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        self._mark(rel)
        return True

    def move(self, src: str, dst: str) -> None:
        """Rename a page. The destination must be free."""
        for rel in (src, dst):
            if is_untouched(rel) or scan.section_of(rel) == scan.JOURNAL:
                raise ApplyRefused(f"the curator never moves {rel}")
        source, target = self.root / src, self.root / dst
        if not source.is_file():
            raise ApplyRefused(f"{src} is not there")
        if target.exists():
            raise ApplyRefused(f"{dst} is taken")
        if not target.parent.is_dir():
            raise ApplyRefused(f"the folder of {dst} is not there")
        self._note_clean(src, self.read(src))
        os.replace(source, target)
        if src in self.clean:
            self.clean.discard(src)
            self.clean.add(dst)
        self._seen.add(dst)
        self.moves[src] = dst
        self._mark(src, dst)

    def trash(self, rel: str) -> str:
        """Move a file to `wiki/.trash/<day>/<rel>` and return the trash path."""
        parts = PurePosixPath(rel).parts
        if not parts or any(p.startswith(".") for p in parts[:-1]):
            raise ApplyRefused(f"the curator never moves {rel}")
        if len(parts) > 1 and parts[0] in ("people", "joshua-docs"):
            raise ApplyRefused(f"the curator never moves {rel}")
        if parts[0] == scan.JOURNAL and len(parts) > 1 and not parts[-1].startswith("."):
            raise ApplyRefused(f"the curator never moves a journal entry: {rel}")
        source = self.root / rel
        if not source.is_file():
            raise ApplyRefused(f"{rel} is not there")
        dest = layout.trash_path(rel, self.day, self.root.parent)
        if dest.exists():
            raise ApplyRefused(f"the trash already holds {rel} for {self.day.isoformat()}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, dest)
        trash_rel = dest.relative_to(self.root).as_posix()
        self.moves[rel] = None
        self.clean.discard(rel)
        self._mark(rel, trash_rel)
        return trash_rel

    def track(self, rel: str) -> bool:
        """Write the tracking keys on a page. Returns False when the page may
        not have them, its frontmatter is not a mapping, or they are current."""
        path = self.root / rel
        if not path.is_file():
            return False
        text = self.read(rel)
        if not may_track(rel, text) or not needs_review(text):
            return False
        try:
            new = with_tracking(text, self.day)
        except ValueError:
            return False
        return self.write(rel, new)


# -- link rewriting ---------------------------------------------------------


def _rewrite_links(
    tree: Tree,
    file_moves: dict[str, str | None],
    targets: dict[str, str],
    texts: dict[str, str],
) -> dict[str, str]:
    """The text of each page after its links follow `targets`.

    `file_moves` is what happens to the files, `targets` where a link to a
    moved page must now go, and `texts` the text of a page that differs from
    the disk. Returns the pages whose text differs from the disk, keyed by
    their path before the move. A journal entry is left as it is.
    """
    after_tree = tree.renamed(file_moves)
    out: dict[str, str] = {}
    for page in scan.link_sources(tree):
        if scan.section_of(page) == scan.JOURNAL:
            continue
        page_after = file_moves.get(page, page)
        if page_after is None:
            continue
        original = tree.text(page)
        text = texts.get(page, original)
        edits: list[tuple[scan.Link, str]] = []
        for link in parse_links(text):
            where = resolve_link(link, page, tree)
            if not where.checked or where.path is None:
                continue
            desired = targets.get(where.path, where.path)
            if resolve_link(link, page_after, after_tree).path == desired:
                continue
            edits.append((link, target_for(link, page_after, desired)))
        for link, new_target in sorted(edits, key=lambda e: e[0].start, reverse=True):
            text = scan.retarget(text, link, new_target)
        if text != original:
            out[page] = text
    return out


def _links_to(text: str, source: str, target: str, tree: Tree) -> bool:
    return any(resolve_link(link, source, tree).path == target for link in parse_links(text))


def _drop_lines_to(text: str, source: str, target: str, tree: Tree) -> str:
    """`text` without each list line whose links all go to `target`."""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        links = parse_links(line)
        if (
            links
            and scan.LIST_LINE_RE.match(line)
            and all(resolve_link(link, source, tree).path == target for link in links)
        ):
            continue
        out.append(line)
    return "".join(out)


def add_index_line(text: str, line: str) -> str:
    """`text` with `line` at the end of its `## Pages` section, which is added at
    the end of the page when it is not there."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, ln in enumerate(lines) if ln.rstrip("\r\n").strip() == scan.PAGES_HEADING),
        None,
    )
    if start is None:
        body = text.rstrip("\r\n")
        sep = newline * 2 if body else ""
        return f"{body}{sep}{scan.PAGES_HEADING}{newline}{newline}{line}{newline}"
    end = len(lines)
    for i in range(start + 1, len(lines)):
        heading = scan.HEADING_RE.match(lines[i])
        if heading and len(heading.group(1)) <= 2:
            end = i
            break
    last = start
    for i in range(start + 1, end):
        if lines[i].strip():
            last = i
    if not lines[last].endswith(("\n", "\r")):
        lines[last] += newline
    if last == start:
        lines.insert(last + 1, newline)
        last += 1
    lines.insert(last + 1, f"{line}{newline}")
    return "".join(lines)


# -- the actions ------------------------------------------------------------


def _relink(ws: Workspace, patch: dict[str, Any]) -> None:
    rel, old, new = patch["path"], patch["old"], patch["new"]
    text = ws.read(rel)
    head, body = _body(text)
    if old not in body:
        raise ApplyRefused(f"the link {old} is no longer in {rel}")
    ws.write(rel, head + body.replace(old, new))


def _drop_line(ws: Workspace, patch: dict[str, Any]) -> None:
    rel, line = patch["path"], patch["line"]
    text = ws.read(rel)
    lines = text.splitlines(keepends=True)
    kept = [ln for ln in lines if ln.rstrip("\r\n") != line]
    if len(kept) == len(lines):
        raise ApplyRefused(f"the line is no longer in {rel}")
    ws.write(rel, "".join(kept))


def _index_add(ws: Workspace, patch: dict[str, Any]) -> None:
    rel, page, line = patch["path"], patch["page"], patch["line"]
    tree = load_tree(ws.root)
    if page not in tree.pages:
        raise ApplyRefused(f"{page} is not there")
    if rel not in tree.pages:
        raise ApplyRefused(f"the index page {rel} is not there")
    text = ws.read(rel)
    if _links_to(text, rel, page, tree):
        return
    ws.write(rel, add_index_line(text, line))


def _trash(ws: Workspace, patch: dict[str, Any]) -> None:
    ws.trash(patch["path"])


def _set_date(ws: Workspace, patch: dict[str, Any]) -> None:
    rel = patch["path"]
    text = ws.read(rel)
    meta, _ = frontmatter.split(text)
    if "date" in meta:
        raise ApplyRefused(f"{rel} has a date")
    try:
        new = frontmatter.update(text, date=date.fromisoformat(patch["date"]))
    except ValueError as exc:
        raise ApplyRefused(f"cannot set the date of {rel}: {exc}") from exc
    ws.write(rel, new)


def _move(ws: Workspace, patch: dict[str, Any]) -> None:
    src, dst = patch["path"], patch["to"]
    tree = load_tree(ws.root)
    if src not in tree.pages:
        raise ApplyRefused(f"{src} is not there")
    if dst in tree.files or (ws.root / dst).exists():
        raise ApplyRefused(f"{dst} is taken")
    if not (ws.root / dst).parent.is_dir():
        raise ApplyRefused(f"the folder of {dst} is not there")
    if is_untouched(dst) or scan.section_of(dst) in scan.OUT_OF_SCOPE:
        raise ApplyRefused(f"the curator never moves a page into {dst}")
    old_index = tree.index_of_folder(PurePosixPath(src).parent.as_posix())
    new_index = tree.index_of_folder(PurePosixPath(dst).parent.as_posix())
    texts: dict[str, str] = {}
    if old_index and old_index != src:
        texts[old_index] = _drop_lines_to(tree.text(old_index), old_index, src, tree)
    rewritten = _rewrite_links(tree, {src: dst}, {src: dst}, texts)
    for rel, text in texts.items():
        rewritten.setdefault(rel, text)
    if new_index and new_index != src:
        after = tree.renamed({src: dst})
        text = rewritten.get(new_index, tree.text(new_index))
        if not _links_to(text, new_index, dst, after):
            title = scan.page_title(tree.text(src), PurePosixPath(src).stem)
            text = add_index_line(text, f"- [{title}](/wiki/{scan.encode_target(dst)})")
            rewritten[new_index] = text
    ws.move(src, dst)
    if src in rewritten:
        ws.write(dst, rewritten.pop(src))
    for rel, text in sorted(rewritten.items()):
        ws.write(rel, text)


def _merge(ws: Workspace, patch: dict[str, Any]) -> None:
    src, into = patch["path"], patch["into"]
    tree = load_tree(ws.root)
    if src not in tree.pages:
        raise ApplyRefused(f"{src} is not there")
    if into not in tree.pages:
        raise ApplyRefused(f"{into} is not there")
    if src == into:
        raise ApplyRefused("a page cannot merge into itself")
    if scan.section_of(src) in scan.OUT_OF_SCOPE:
        raise ApplyRefused(f"the curator never merges {src}")
    texts: dict[str, str] = {}
    for rel in sorted(tree.pages):
        if not tree.is_index_page(rel) or rel == src or is_untouched(rel):
            continue
        text = tree.text(rel)
        if _links_to(text, rel, src, tree) and _links_to(text, rel, into, tree):
            texts[rel] = _drop_lines_to(text, rel, src, tree)
    rewritten = _rewrite_links(tree, {src: None}, {src: into}, texts)
    for rel, text in texts.items():
        rewritten.setdefault(rel, text)
    ws.trash(src)
    for rel, text in sorted(rewritten.items()):
        ws.write(rel, text)


def _refresh(ws: Workspace, patch: dict[str, Any]) -> None:
    rel, pairs = patch["path"], patch.get("patch") or []
    if not pairs:
        raise ApplyRefused("the patch is empty")
    text = ws.read(rel)
    if not text:
        raise ApplyRefused(f"{rel} is not there")
    head, body = _body(text)
    for pair in pairs:
        find, replace = pair.get("find", ""), pair.get("replace", "")
        if not find or body.count(find) != 1:
            raise ApplyRefused(f"a find text does not occur exactly once in {rel}")
        body = body.replace(find, replace, 1)
    ws.write(rel, head + body)


_HANDLERS = {
    "relink": _relink,
    "drop-line": _drop_line,
    "index-add": _index_add,
    "trash": _trash,
    "set-date": _set_date,
    "move": _move,
    "merge": _merge,
    "refresh": _refresh,
}


def apply_patch(ws: Workspace, patch: dict[str, Any]) -> None:
    """Apply one patch to the wiki. Raises `ApplyRefused` when it does not fit."""
    action = patch.get("action")
    handler = _HANDLERS.get(action) if isinstance(action, str) else None
    if handler is None:
        raise ApplyRefused(f"unknown action: {action!r}")
    try:
        handler(ws, patch)
    except KeyError as exc:
        raise ApplyRefused(f"the patch has no {exc.args[0]}") from exc


def finds_once(text: str, pairs: list[dict[str, Any]]) -> bool:
    """True when each `find`, in order, occurs exactly once in the body of `text`
    as the patch reaches it."""
    _, body = _body(text)
    for pair in pairs:
        find = pair.get("find", "")
        if not find or body.count(find) != 1:
            return False
        body = body.replace(find, pair.get("replace", ""), 1)
    return True
