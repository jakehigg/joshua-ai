"""The documents on the data volume, as the viewer reads them.

A document is a plan, a report, a spec, a note, or an artifact that a person
and Joshua work on. It is one file under ``/data/documents/<owner>/``, where
the owner is a member's person id or ``shared``: ``<slug>.md`` for Markdown or
``<slug>.html`` for HTML, with YAML frontmatter at the top (``title``,
``kind``, ``status``, ``format``, ``summary``, ``updated``, and more).
``/data/documents`` is a git repository, so each version of a file is a
commit.

The comments of a document are in the comments file beside it,
``<slug>.comments.jsonl``: one JSON object a line, with ``id``,
``line_no``, ``line_text``, ``body``, ``author``, ``created_at``,
``resolved_at``, and ``resolution``. ``line_no`` is a line of the body, from
1 at the first line after the closing ``---`` of the frontmatter, so a change
of the frontmatter never moves a comment. ``line_no`` 0 is the document as a
whole, and its ``line_text`` is the ``title:`` line. ``stale`` is not stored:
it is true when that line is no longer ``line_text``.

A member reads their own folder and ``shared/``, and no other member's
folder. :func:`find` refuses an owner or a slug that is not of the allowed
form, an owner that is not allowed, and a path that leaves the folder of the
owner. The viewer writes one thing here: a comment, added or deleted by its
author, in the comments file, and each write is one commit with the author
Joshua. Nothing here creates the root.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from joshua_shared import layout, wikigit
from joshua_shared.ids import PERSON_ID_RE
from joshua_shared.log import get_logger

logger = get_logger("viewer.documents")

SHARED = "shared"
FORMATS = ("md", "html")
COMMENTS_SUFFIX = ".comments.jsonl"
STATUSES = ("draft", "active", "final", "archived")
KINDS = ("plan", "report", "spec", "artifact", "note")
DEFAULT_KIND = "note"
DEFAULT_STATUS = "draft"

# A slug is one file name with no extension: no dot at the start, no path.
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
# A slug never ends in one of these: ``<slug>.comments`` names the comments
# file of a document, and ``<slug>.json`` names its frontmatter route in the
# viewer.
RESERVED_SUFFIXES = (".comments", ".json")
_REF_RE = re.compile(r"^/?documents/(?P<owner>[^/]+)/(?P<slug>[^/]+)\.(?P<ext>md|html)\Z")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}\Z")
_GIT_TIMEOUT_S = 10
# The most versions that the page lists.
MAX_VERSIONS = 50
# The longest ``line_text`` that a comment keeps.
MAX_LINE_TEXT = 1000


def root() -> Path:
    """``<data>/documents``."""
    return layout.documents_root(layout.data_root())


def owners(person_id: str) -> tuple[str, ...]:
    """The folders that a person reads: their own and ``shared``."""
    return (person_id, SHARED)


def valid_owner(owner: str) -> bool:
    return owner == SHARED or bool(PERSON_ID_RE.match(owner))


def valid_slug(slug: str) -> bool:
    return bool(SLUG_RE.match(slug)) and ".." not in slug and not slug.endswith(RESERVED_SUFFIXES)


def document_url(ref: str) -> str | None:
    """The viewer page of a document path such as
    ``documents/<owner>/<slug>.md``, or None when ``ref`` is not one."""
    match = _REF_RE.match(ref.strip())
    if match is None:
        return None
    owner, slug = match.group("owner"), match.group("slug")
    if not valid_owner(owner) or not valid_slug(slug):
        return None
    return f"/documents/{owner}/{slug}"


# -- one document -----------------------------------------------------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _iso(value: Any) -> str | None:
    """A time from the frontmatter as ISO text with an offset, or None."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return moment.isoformat()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC).isoformat()
    if isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return (moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)).isoformat()
    return None


def split(text: str) -> tuple[dict[str, Any], str, int]:
    """The frontmatter, the body, and the number of lines before the body.

    The frontmatter is the lines between a first line ``---`` and the next
    ``---``. With no closing line, or YAML that is not a mapping, the whole
    text is the body."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text, 0
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            try:
                data = yaml.safe_load("\n".join(lines[1:index]))
            except yaml.YAMLError:
                return {}, text, 0
            if not isinstance(data, dict):
                data = {}
            return data, "\n".join(lines[index + 1 :]), index + 1
    return {}, text, 0


def first_paragraph(body: str) -> str:
    """The first paragraph of Markdown that is not a heading, on one line."""
    out: list[str] = []
    fence = False
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fence = not fence
            if out:
                break
            continue
        if fence:
            continue
        if not stripped:
            if out:
                break
            continue
        if not out and (stripped.startswith(("#", "---", "|", "<")) or stripped == "***"):
            continue
        out.append(stripped)
    return " ".join(out)[:300]


@dataclass
class Document:
    """One document file and what its frontmatter says."""

    owner: str
    slug: str
    ext: str
    path: Path
    text: str
    front: dict[str, Any]
    body: str
    offset: int
    mtime: float = 0.0

    @property
    def tool_path(self) -> str:
        """The path as Joshua's tools name it: ``documents/<owner>/<slug>.<ext>``."""
        return f"documents/{self.owner}/{self.slug}.{self.ext}"

    @property
    def url(self) -> str:
        return f"/documents/{self.owner}/{self.slug}"

    @property
    def rel(self) -> str:
        """The path inside the documents repository."""
        return f"{self.owner}/{self.slug}.{self.ext}"

    @property
    def lines(self) -> list[str]:
        """The lines of the stored file, frontmatter included."""
        return self.text.split("\n")

    @property
    def body_lines(self) -> list[str]:
        """The lines of the body. A comment counts its ``line_no`` here."""
        return self.body.split("\n")

    @property
    def title_line(self) -> str:
        """The ``title:`` line of the frontmatter, the anchor of line 0, or ""."""
        head = self.lines[1 : max(self.offset - 1, 1)]
        return next((line for line in head if line.startswith("title:")), "")

    def line(self, line_no: int) -> str | None:
        """The text that a comment on ``line_no`` anchors to: the ``title:``
        line for 0, a line of the body from 1, else None."""
        if line_no == 0:
            return self.title_line
        body = self.body_lines
        return body[line_no - 1] if 1 <= line_no <= len(body) else None

    @property
    def title(self) -> str:
        return _text(self.front.get("title")).strip() or self.slug

    @property
    def kind(self) -> str:
        return _text(self.front.get("kind")).strip().lower() or DEFAULT_KIND

    @property
    def status(self) -> str:
        return _text(self.front.get("status")).strip().lower() or DEFAULT_STATUS

    @property
    def format(self) -> str:
        if self.ext == "html":
            return "html"
        return "html" if _text(self.front.get("format")).strip().lower() == "html" else "markdown"

    @property
    def scripts(self) -> bool:
        """True only when the frontmatter says ``scripts: true``."""
        return self.front.get("scripts") is True

    @property
    def summary(self) -> str:
        given = " ".join(_text(self.front.get("summary")).split())
        if given or self.format == "html":
            return given
        return first_paragraph(self.body)

    @property
    def tags(self) -> list[str]:
        raw = self.front.get("tags")
        return [_text(t) for t in raw] if isinstance(raw, list) else []

    @property
    def updated(self) -> str | None:
        """The ``updated`` time, else the time of the file."""
        given = _iso(self.front.get("updated"))
        if given is not None:
            return given
        if self.mtime:
            return datetime.fromtimestamp(self.mtime, UTC).isoformat()
        return None

    @property
    def assignment(self) -> str:
        return _text(self.front.get("assignment")).strip()

    @property
    def promoted_to(self) -> str:
        return _text(self.front.get("promoted_to")).strip()

    @property
    def author(self) -> str:
        return _text(self.front.get("author")).strip()


def _load(owner: str, slug: str, ext: str, path: Path) -> Document | None:
    try:
        text = path.read_text(encoding="utf-8")
        mtime = path.stat().st_mtime
    except (OSError, UnicodeDecodeError):
        return None
    front, body, offset = split(text)
    return Document(owner, slug, ext, path, text, front, body, offset, mtime)


def _folder(base: Path, owner: str) -> Path | None:
    """The folder of ``owner`` under ``base``, when it is a real folder in it."""
    folder = base / owner
    try:
        if folder.is_symlink() or not folder.is_dir():
            return None
        folder.resolve().relative_to(base.resolve())
    except (OSError, ValueError):
        return None
    return folder


def find(base: Path, owner: str, slug: str, allowed: Iterable[str]) -> Document | None:
    """The document ``<owner>/<slug>``, or None when the owner is not allowed,
    a name is not of the allowed form, or there is no such file. Markdown
    comes before HTML when both are there."""
    if owner not in set(allowed) or not valid_owner(owner) or not valid_slug(slug):
        return None
    folder = _folder(base, owner)
    if folder is None:
        return None
    for ext in FORMATS:
        path = folder / f"{slug}.{ext}"
        try:
            if path.is_symlink() or not path.is_file():
                continue
            path.resolve().relative_to(folder.resolve())
        except (OSError, ValueError):
            continue
        return _load(owner, slug, ext, path)
    return None


def list_documents(base: Path, allowed: Iterable[str]) -> list[Document]:
    """Every document of the allowed owners, the newest ``updated`` first."""
    found: list[Document] = []
    for owner in dict.fromkeys(allowed):
        if not valid_owner(owner):
            continue
        folder = _folder(base, owner)
        if folder is None:
            continue
        seen: set[str] = set()
        for ext in FORMATS:
            for path in sorted(folder.glob(f"*.{ext}")):
                slug = path.name[: -len(ext) - 1]
                if slug in seen or not valid_slug(slug) or path.is_symlink():
                    continue
                if not path.is_file():
                    continue
                doc = _load(owner, slug, ext, path)
                if doc is not None:
                    seen.add(slug)
                    found.append(doc)
    found.sort(key=lambda d: d.updated or "", reverse=True)
    return found


# -- comments ---------------------------------------------------------------


@dataclass
class DocComment:
    """One line of a comments file, with ``stale`` worked out on read."""

    id: Any
    line_no: int
    line_text: str = ""
    body: str = ""
    author: str = ""
    created_at: str | None = None
    resolved_at: str | None = None
    resolution: str | None = None
    stale: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def comments_path(doc: Document) -> Path:
    return doc.path.with_name(f"{doc.slug}{COMMENTS_SUFFIX}")


def _parse_comment(line: str, doc: Document) -> DocComment | None:
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("id") is None:
        return None
    raw_no = data.get("line_no")
    if isinstance(raw_no, bool) or raw_no is None:
        return None
    try:
        line_no = int(raw_no)
    except (TypeError, ValueError):
        return None
    line_text = _text(data.get("line_text"))
    current = doc.line(line_no)
    return DocComment(
        id=data["id"],
        line_no=line_no,
        line_text=line_text,
        body=_text(data.get("body")),
        author=_text(data.get("author")),
        created_at=_iso(data.get("created_at")),
        resolved_at=_iso(data.get("resolved_at")) or (_text(data.get("resolved_at")) or None),
        resolution=_text(data.get("resolution")) or None,
        stale=current != line_text,
        raw=data,
    )


def read_comments(doc: Document) -> list[DocComment]:
    """The comments of ``doc``, in the order of the file. A line that is not
    a comment is skipped."""
    try:
        raw = comments_path(doc).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in raw.split("\n"):
        if line.strip():
            comment = _parse_comment(line, doc)
            if comment is not None:
                out.append(comment)
    return out


def open_count(doc: Document) -> int:
    return sum(1 for c in read_comments(doc) if c.resolved_at is None)


def _next_id(comments: list[DocComment]) -> int:
    numbers = [c.id for c in comments if isinstance(c.id, int) and not isinstance(c.id, bool)]
    taken = {str(c.id) for c in comments}
    candidate = max(numbers, default=0) + 1
    while str(candidate) in taken:
        candidate += 1
    return candidate


def add_comment(
    base: Path,
    doc: Document,
    *,
    author: str,
    line_no: int,
    line_text: str,
    body: str,
    now: datetime,
) -> int:
    """Append one comment of ``author`` to the comments file of ``doc`` and
    commit it. Returns its id."""
    path = comments_path(doc)
    comment_id = _next_id(read_comments(doc))
    record = {
        "id": comment_id,
        "line_no": line_no,
        "line_text": line_text[:MAX_LINE_TEXT],
        "body": body,
        "author": author,
        "created_at": now.astimezone(UTC).isoformat(),
        "resolved_at": None,
        "resolution": None,
    }
    try:
        before = path.read_bytes()
    except FileNotFoundError:
        before = b""
    prefix = "" if not before or before.endswith(b"\n") else "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(prefix + json.dumps(record, ensure_ascii=False) + "\n")
    commit(base, [path], f"viewer: comment {doc.tool_path}")
    return comment_id


def delete_comment(base: Path, doc: Document, *, author: str, comment_id: str) -> bool:
    """Remove the open comment ``comment_id`` of ``author`` from the comments
    file and commit it. False when there is no such open comment of theirs."""
    path = comments_path(doc)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    kept: list[str] = []
    removed = False
    for line in raw.split("\n"):
        comment = _parse_comment(line, doc) if line.strip() else None
        if (
            not removed
            and comment is not None
            and str(comment.id) == comment_id
            and comment.author == author
            and comment.resolved_at is None
        ):
            removed = True
            continue
        kept.append(line)
    if not removed:
        return False
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text("\n".join(kept), encoding="utf-8")
    os.replace(tmp, path)
    commit(base, [path], f"viewer: comment {doc.tool_path}")
    return True


# -- git --------------------------------------------------------------------


def commit(base: Path, paths: list[Path], message: str) -> bool:
    """Commit ``paths`` in the documents repository, as Joshua. With no
    repository there, nothing is committed: the viewer creates none."""
    if not (base / ".git").exists():
        logger.warning({"message": "documents have no git repository; no commit"})
        return False
    return wikigit.commit(base, paths, message)


@dataclass(frozen=True)
class Version:
    """One commit that changed a document."""

    sha: str
    when: str
    subject: str


def _git(base: Path, args: list[str]) -> str | None:
    if not (base / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={base}", *args],
            cwd=base,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning({"message": "documents git failed", "error": type(exc).__name__})
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def versions(base: Path, doc: Document) -> list[Version]:
    """The commits that changed the file of ``doc``, the newest first."""
    out = _git(base, ["log", f"-n{MAX_VERSIONS}", "--format=%H%x1f%aI%x1f%s", "--", doc.rel])
    found = []
    for line in (out or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3 and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            found.append(Version(parts[0], parts[1], parts[2]))
    return found


def at_version(base: Path, doc: Document, rev: str) -> tuple[Version, Document] | None:
    """``doc`` as it was at the commit ``rev``, a sha or a prefix of one of
    its versions. None for any other value."""
    if not _SHA_RE.match(rev):
        return None
    matches = [v for v in versions(base, doc) if v.sha.startswith(rev)]
    if len(matches) != 1:
        return None
    version = matches[0]
    text = _git(base, ["show", f"{version.sha}:{doc.rel}"])
    if text is None:
        return None
    front, body, offset = split(text)
    old = Document(doc.owner, doc.slug, doc.ext, doc.path, text, front, body, offset)
    return version, old
