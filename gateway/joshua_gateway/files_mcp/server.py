"""The files MCP server, hosted in-process by the gateway as the ``files`` builtin.

It lets the agent list, read, search, write, and rename files in the corpus: the
one wiki under ``wiki/`` (a member writes, a guest reads);
a journal post under ``people/<person>/blog/`` (create or append, never
overwrite); ``people/<person>/profile.md`` and
``people/<person>/attachments/`` for reading; and ``shared/`` (the shared
profile and the group attachments, read-only). It is the agent's only file
interface. It replaces the SDK ``Read``/``Write``/``Edit`` tools, so the agent
reaches exactly these paths and nothing else.

**One corpus.** The wiki is what Joshua knows, the journal is when something
happened, and an attachment is the artifact. No root is keyed on a person: the
role of the request decides the write, and ``people/<person>/`` is provenance.
A member writes the wiki and the journal. A guest writes nothing, and a request
with no role writes nothing.

The role comes from the request (``role_ctx``), never from a tool argument.
``paths.resolve`` confines every path to the root set of the request. A path violation
returns a tool error with a plain message; it never echoes the absolute path.

There is no ``delete_file``. Deletion is a human action through the viewer or a
shell.
"""

from __future__ import annotations

import base64
import json
import os
import posixpath
import re
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

import mcp_types as types
import yaml
from joshua_shared.layout import is_hidden
from mcp.server.lowlevel import Server
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from joshua_gateway.files_mcp.paths import (
    DIR_ROOTS,
    UNKNOWN,
    PathError,
    Root,
    resolve,
)
from joshua_gateway.files_mcp.paths import (
    roots as roots_for_role,
)
from joshua_gateway.observability import person_ctx, role_ctx

# The data volume, fixed at /data in every container; overridable for tests.
DATA_ENV = "JOSHUA_DATA_DIR"
DEFAULT_DATA_ROOT = "/data"

# One write, and one non-image attachment read, is at most this many bytes.
MAX_BYTES = 256 * 1024

# A PDF read extracts text from at most this many pages, and never more than
# ``MAX_BYTES`` of text. A longer PDF is capped and the header says so.
PDF_MAX_PAGES = 20

# The nightly digest name ``blog/YYYY-MM-DD.md`` (no time part) is reserved for
# core; the agent cannot write it.
_BLOG_DIGEST = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

# A blog filename that already carries a valid ``YYYY-MM-DD-HHMM-`` prefix is kept
# as the model set it; any other name gets a server-stamped prefix.
_BLOG_STAMPED = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}-")

# The ``YYYY-MM-DD-HHMMSS-`` prefix on a stored attachment. ``rename_file`` keeps
# this prefix and renames the descriptive part only.
_ATTACHMENT_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6}-)")

# Characters allowed in a stored filename stem (the same rule as channels'
# ``safe_filename``). Everything else becomes ``_``.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")

# The sidecar a channels-stored attachment carries: ``<file>.meta.json``.
_META_SUFFIX = ".meta.json"

# Attachment suffixes returned as an ``ImageContent`` block; channels converts HEIC
# to JPEG on the way in, so a lingering HEIC is not renderable and reads as metadata.
IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class FilesError(Exception):
    """A file operation failed a rule (size, mode, or frontmatter). The message is
    safe to return to the caller."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def data_root() -> Path:
    """The data volume root, from ``JOSHUA_DATA_DIR`` or ``/data``."""
    return Path(os.environ.get(DATA_ENV, "").strip() or DEFAULT_DATA_ROOT)


def build_builtin_server(name: str, options: dict[str, Any]) -> Server:
    """Build the in-process ``Server`` for a builtin catalog entry.

    Only ``files`` exists today; ``viewer`` and ``research`` are reserved names.
    """
    if name == "files":
        from joshua_shared import config

        return build_files_server(data_root(), timezone=config.load().timezone)
    raise ValueError(f"unknown builtin server: {name}")


def _now() -> datetime:
    """The current UTC time. A seam the tests override for a fixed blog clock."""
    return datetime.now(UTC)


TOOLS = [
    types.Tool(
        name="list_files",
        description=(
            "List files under one root. wiki is the one wiki that everyone uses; "
            "wiki/joshua/ holds Joshua's own documentation. people holds every "
            "person's journal, profile, and files, one directory per person id; "
            "shared holds the shared profile and the group files. Pass subpath to "
            "go deeper, such as root people and subpath <person-id>/blog. Returns "
            "[{path, bytes, modified}]; each path is root-relative and accepted by "
            "read_file. An attachment adds original_name when the sender's "
            "filename is known."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "root": {"type": "string", "enum": list(DIR_ROOTS)},
                "subpath": {"type": "string", "default": ""},
                "recursive": {"type": "boolean", "default": False},
            },
            "required": ["root"],
        },
    ),
    types.Tool(
        name="read_file",
        description=(
            "Read one file by its root-relative path, such as wiki/recipes/pizza.md "
            "or attachments/2026/08/a.jpg. An image returns an image block; a PDF "
            "returns its extracted text."
        ),
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    ),
    types.Tool(
        name="write_file",
        description=(
            "Write one .md file under wiki/ or people/<person-id>/blog/. The "
            "person segment is the person id given in your system prompt, never a "
            "display name; a segment that names nobody is refused. mode create "
            "fails if the file exists; overwrite replaces it; append adds to it. A "
            "journal post is create or append only; pass a plain slug such as "
            "people/<person-id>/blog/garden-notes.md and the server stamps the date "
            "and time into the name. Max 256 KB."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["create", "overwrite", "append"],
                    "default": "create",
                },
            },
            "required": ["path", "content"],
        },
    ),
    types.Tool(
        name="rename_file",
        description=(
            "Rename one file in place under wiki/, people/<person-id>/blog/, or "
            "people/<person-id>/attachments/. new_name is a bare filename; the "
            "extension must not change. An attachment keeps its date-time prefix, "
            "so you rename the descriptive part only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "new_name": {"type": "string"},
            },
            "required": ["path", "new_name"],
        },
    ),
    types.Tool(
        name="search_files",
        description=(
            "Case-insensitive substring or regex search over markdown text in one "
            "root. Returns [{path, line, snippet}]. This is not semantic search."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "root": {"type": "string", "enum": list(DIR_ROOTS), "default": "wiki"},
                "max_results": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    ),
]


def _role_from_config(person: str) -> str | None:
    """The configured role of ``person``, or None when unknown."""
    from joshua_shared import config

    entry = config.load().person(person)
    return entry.role if entry is not None else None


def _persons_from_config() -> frozenset[str]:
    """The person ids on the roster. Read per request, so a reload is seen."""
    from joshua_shared import config

    return frozenset(entry.id for entry in config.load().people)


def build_files_server(
    root_dir: Path,
    *,
    timezone: str = "UTC",
    role_for: Callable[[str], str | None] | None = None,
    persons: Callable[[], frozenset[str]] | None = None,
) -> Server:
    """Build the files ``Server`` for a data volume at ``root_dir``.

    ``timezone`` is the configured timezone; the server stamps a journal post
    name in it. ``role_for`` returns a person's role, and it is the fallback for
    a request that carries a person but no role header. ``persons`` returns the
    roster, which says which person segment a write below ``people`` may name.
    """
    tz = ZoneInfo(timezone)
    roles = role_for or _role_from_config
    roster = persons or _persons_from_config

    def roots_for_request() -> dict[str, Root]:
        """The root set of this request, from the role and from the person.

        The corpus is shared, so the role alone decides what a request reads
        and whether it writes. ``role_ctx`` holds what core asserted. A core
        that sends no role header falls back to the role of the person it
        names; a request with neither reads and writes nothing.

        The person decides one thing more: a write below ``people`` needs a
        person segment that names somebody, and a request core could not
        attribute has no such segment to offer.
        """
        person = person_ctx.get()
        role = role_ctx.get()
        if role is None:
            if person is not None and person != UNKNOWN:
                role = roles(person)
        return roots_for_role(root_dir, role=role or "", people=roster(), person=person)

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(tools=TOOLS)

    async def on_call_tool(ctx, params):
        args = params.arguments or {}
        try:
            handler = _HANDLERS.get(params.name)
            if handler is None:
                raise FilesError(f"unknown tool: {params.name}")
            return handler(roots_for_request(), args, tz)
        except (PathError, FilesError) as exc:
            return _error(exc.message)

    return Server("files", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


# -- tool handlers ----------------------------------------------------------


def _list_files(roots: dict[str, Root], args: dict[str, Any], tz: ZoneInfo) -> types.CallToolResult:
    root_name = args.get("root", "")
    subpath = (args.get("subpath") or "").strip()
    recursive = bool(args.get("recursive"))
    rel = f"{root_name}/{subpath}" if subpath else root_name
    root, base = resolve(rel, roots, write=False)
    if not base.is_dir():
        return _json([])

    paths = base.rglob("*") if recursive else base.iterdir()
    entries = []
    for item in paths:
        if not item.is_file() or item.name.endswith(_META_SUFFIX):
            continue
        if is_hidden(item, root.base):
            continue
        stat = item.stat()
        entry = {
            "path": f"{root.name}/{item.relative_to(root.base).as_posix()}",
            "bytes": stat.st_size,
            "modified": _iso(stat.st_mtime),
        }
        original = _original_name(item)
        if original is not None:
            entry["original_name"] = original
        entries.append(entry)
    entries.sort(key=lambda entry: entry["path"])
    return _json(entries)


def _read_file(roots: dict[str, Root], args: dict[str, Any], tz: ZoneInfo) -> types.CallToolResult:
    root, abs_path = resolve(args.get("path", ""), roots, write=False)
    if not abs_path.is_file():
        raise FilesError("file not found")
    stat = abs_path.stat()

    if abs_path.suffix.lower() == ".pdf":
        rel = _rel_path(root, abs_path)
        return _read_pdf(abs_path, rel)

    if _people_kind(args.get("path", "")) == "attachments":
        mime = IMAGE_MIME.get(abs_path.suffix.lower())
        if mime is not None:
            data = base64.b64encode(abs_path.read_bytes()).decode("ascii")
            return types.CallToolResult(
                content=[types.ImageContent(type="image", data=data, mime_type=mime)]
            )
        text = _read_utf8(abs_path) if stat.st_size <= MAX_BYTES else None
        if text is None:
            return _json(_metadata(root, abs_path, stat))
        return _text(text)

    text = _read_utf8(abs_path)
    if text is None:
        return _json(_metadata(root, abs_path, stat))
    return _text(text)


def _write_file(roots: dict[str, Root], args: dict[str, Any], tz: ZoneInfo) -> types.CallToolResult:
    content = args.get("content", "")
    mode = args.get("mode", "create")
    if mode not in ("create", "overwrite", "append"):
        raise FilesError("mode must be create, overwrite, or append")
    path = args.get("path", "")
    root, abs_path = resolve(path, roots, write=True)

    if _people_kind(path) == "blog":
        return _write_blog(roots, path, content, mode, tz)

    _check_frontmatter(content)
    data = content.encode("utf-8")
    if mode == "create" and abs_path.exists():
        raise FilesError("file exists")
    if mode == "append" and abs_path.exists():
        data = abs_path.read_bytes() + data
    if len(data) > MAX_BYTES:
        raise FilesError("file exceeds 256 KB")

    _atomic_write(abs_path, data)
    return _json({"path": path, "bytes": len(data)})


def _rename_file(
    roots: dict[str, Root], args: dict[str, Any], tz: ZoneInfo
) -> types.CallToolResult:
    rel = args.get("path", "")
    root, src = resolve(rel, roots, write=False)
    kind = _people_kind(rel)
    if root.name == "wiki":
        pass
    elif root.name == "people" and kind in ("blog", "attachments"):
        pass
    else:
        raise FilesError(f"cannot rename in {root.name}")
    if not root.can_write:
        raise FilesError(f"{root.name} is read-only for a guest")
    if not src.is_file():
        raise FilesError("file not found")

    new_name = str(args.get("new_name", ""))
    if not new_name.strip() or "/" in new_name or "\\" in new_name or ".." in new_name:
        raise FilesError("new_name must be a bare filename")

    new_ext = Path(new_name).suffix
    if new_ext.lower() != src.suffix.lower():
        raise FilesError("the extension must not change")
    stem = _sanitize_stem(Path(new_name).stem)
    if not stem:
        raise FilesError("new_name is empty after sanitizing")

    if kind == "attachments":
        prefix = _attachment_prefix(src.name)
        final = f"{prefix}{stem}{src.suffix}"
    else:
        final = f"{stem}{src.suffix}"

    dest = src.parent / final
    if dest == src:
        raise FilesError("new_name matches the current name")
    if dest.exists():
        raise FilesError("target exists")

    os.rename(src, dest)
    return _json({"path": _rel_path(root, dest), "renamed_from": _rel_path(root, src)})


def _search_files(
    roots: dict[str, Root], args: dict[str, Any], tz: ZoneInfo
) -> types.CallToolResult:
    query = args.get("query", "")
    root_name = args.get("root", "wiki")
    max_results = int(args.get("max_results", 20))
    root, base = resolve(root_name, roots, write=False)
    pattern = _compile(query)

    hits = []
    if base.is_dir():
        for item in sorted(base.rglob("*.md")):
            if not item.is_file() or is_hidden(item, root.base):
                continue
            text = _read_utf8(item)
            if text is None:
                continue
            rel = f"{root.name}/{item.relative_to(root.base).as_posix()}"
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    hits.append({"path": rel, "line": number, "snippet": line.strip()[:200]})
                    if len(hits) >= max_results:
                        return _json(hits)
    return _json(hits)


_HANDLERS = {
    "list_files": _list_files,
    "read_file": _read_file,
    "write_file": _write_file,
    "rename_file": _rename_file,
    "search_files": _search_files,
}


# -- blog write -------------------------------------------------------------


def _write_blog(
    roots: dict[str, Root], path: str, content: str, mode: str, tz: ZoneInfo
) -> types.CallToolResult:
    """Write one blog post. create or append only; the server stamps the name.

    ``blog/<slug>.md`` becomes ``blog/YYYY-MM-DD-HHMM-<slug>.md`` in the configured
    timezone, unless the name already carries a valid stamp. The reserved digest
    name ``blog/YYYY-MM-DD.md`` is refused. Frontmatter is injected when absent.
    """
    if mode == "overwrite":
        raise FilesError("a blog post is append-only; write a new post or append")

    parts = PurePosixPath(path.strip()).parts
    if len(parts) < 4:
        person = roots["people"].request_person
        where = f"people/{person}/blog/" if person else "people/<person-id>/blog/"
        raise FilesError(f"a journal post needs a person and a filename, under {where}")
    name = parts[-1]
    if _BLOG_DIGEST.match(name):
        raise FilesError("people/<person>/blog/YYYY-MM-DD.md is reserved for the nightly digest")

    now_local = _now().astimezone(tz)
    stamped = name if _BLOG_STAMPED.match(name) else _stamp_blog_name(name, now_local)
    # Keep every segment but the filename, so the post stays in the journal of
    # the person the path named.
    new_rel = "/".join([*parts[:-1], stamped])
    root, abs_path = resolve(new_rel, roots, write=True)

    if mode == "append" and abs_path.exists():
        # A continuation gets no injected frontmatter; only its own is validated.
        _validate_attachments((_parse_frontmatter(content) or {}).get("attachments"), roots)
        data = abs_path.read_bytes() + content.encode("utf-8")
    else:
        # The author is the person in the path. A group turn carries no person
        # in the header, and the post still belongs to somebody.
        body = _blog_frontmatter(content, _people_person(path), now_local, roots)
        data = body.encode("utf-8")
        if mode == "create" and abs_path.exists():
            abs_path = _dedupe_path(abs_path)
    if len(data) > MAX_BYTES:
        raise FilesError("file exceeds 256 KB")

    _atomic_write(abs_path, data)
    return _json({"path": _rel_path(root, abs_path), "bytes": len(data)})


def _stamp_blog_name(name: str, now_local: datetime) -> str:
    """Return ``YYYY-MM-DD-HHMM-<slug>.md`` from a bare ``<slug>.md`` name."""
    stem = _sanitize_stem(Path(name).stem) or "post"
    return f"{now_local:%Y-%m-%d-%H%M}-{stem}.md"


def _blog_frontmatter(
    content: str, person: str | None, now_local: datetime, roots: dict[str, Root]
) -> str:
    """Return ``content`` with blog frontmatter.

    When the content has no frontmatter, a block with ``date``, ``person``,
    ``source: chat``, and an empty ``attachments`` list is prepended. When it has
    frontmatter, each listed ``attachments`` path is validated to exist under the
    person's ``attachments/`` root.
    """
    fm = _parse_frontmatter(content)
    if fm is None:
        block = yaml.safe_dump(
            {
                "date": now_local.isoformat(),
                "person": person or "unknown",
                "source": "chat",
                "attachments": [],
            },
            sort_keys=False,
        ).strip()
        return f"---\n{block}\n---\n\n{content}"

    _validate_attachments(fm.get("attachments"), roots)
    return content


def _validate_attachments(listed: Any, roots: dict[str, Root]) -> None:
    """Check each frontmatter attachment path names a stored attachment.

    A path is ``people/<person>/attachments/...``. The check is that the file
    exists and sits in the attachments of somebody, so a post cannot point at a
    wiki page or at a journal entry and call it a photo.
    """
    if listed in (None, []):
        return
    if not isinstance(listed, list):
        raise FilesError("frontmatter attachments must be a list")
    for item in listed:
        rel = str(item)
        try:
            root, abs_path = resolve(rel, roots, write=False)
        except PathError as exc:
            raise FilesError(f"attachment not found: {rel}") from exc
        if _people_kind(rel) != "attachments" or not abs_path.is_file():
            raise FilesError(f"attachment not found: {rel}")


def _parse_frontmatter(content: str) -> dict[str, Any] | None:
    """Return the parsed leading YAML frontmatter, or None when there is none.

    A ``---`` opener with no closing fence is a markdown horizontal rule, not
    frontmatter, so it returns None.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            try:
                data = yaml.safe_load("\n".join(lines[1:index]))
            except yaml.YAMLError as exc:
                raise FilesError("invalid YAML frontmatter") from exc
            return data if isinstance(data, dict) else {}
    return None


# -- PDF read ---------------------------------------------------------------


def _read_pdf(abs_path: Path, rel: str) -> types.CallToolResult:
    """Return the extracted text of a PDF, capped at ``PDF_MAX_PAGES`` and 256 KB.

    A file that pypdf cannot open falls back to a metadata note. The header names
    the file as a PDF and says when the text is capped.
    """
    try:
        reader = PdfReader(str(abs_path))
        total = len(reader.pages)
        chunks: list[str] = []
        size = 0
        used = 0
        for page in reader.pages[:PDF_MAX_PAGES]:
            text = (page.extract_text() or "") + "\n"
            encoded = len(text.encode("utf-8"))
            if size + encoded > MAX_BYTES:
                break
            chunks.append(text)
            size += encoded
            used += 1
    except (PyPdfError, ValueError, OSError):
        stat = abs_path.stat()
        return _json(
            {
                "path": rel,
                "bytes": stat.st_size,
                "modified": _iso(stat.st_mtime),
                "note": "PDF; text could not be extracted",
            }
        )

    header = f"PDF {rel}: {total} page(s)"
    if used < total:
        header += f"; text from the first {used} page(s) (capped)"
    return _text(f"{header}\n\n{''.join(chunks)}")


# -- helpers ----------------------------------------------------------------


def _people_kind(path: str) -> str | None:
    """The kind a ``people/<person>/<kind>/...`` path names, or None.

    The corpus is one tree, so a handler asks what a path *is* rather than
    which root it came from. ``people/alex/blog/x.md`` gives ``blog``;
    ``people/alex/attachments/2026/08/a.jpg`` gives ``attachments``.
    """
    parts = PurePosixPath(path.strip()).parts
    if len(parts) >= 3 and parts[0] == "people":
        return parts[2]
    return None


def _people_person(path: str) -> str | None:
    """The person a ``people/<person>/...`` path names, or None.

    The author of a journal post comes from the path, not from the request. A
    group turn carries no person, and the post still belongs to somebody.
    """
    parts = PurePosixPath(path.strip()).parts
    if len(parts) >= 2 and parts[0] == "people":
        return parts[1]
    return None


def _rel_path(root: Root, abs_path: Path) -> str:
    """Return the root-relative path of ``abs_path`` for a listing or a result."""
    if root.is_file:
        return root.name
    return f"{root.name}/{abs_path.relative_to(root.base).as_posix()}"


def _sanitize_stem(raw: str) -> str:
    """Return a safe filename stem (channels' ``safe_filename`` stem rule)."""
    stem = posixpath.basename(str(raw).replace("\\", "/")).strip()
    return _UNSAFE_CHARS.sub("_", stem)[:48].strip("._-")


def _attachment_prefix(name: str) -> str:
    """Return the ``YYYY-MM-DD-HHMMSS-`` prefix of a stored attachment, or ``""``."""
    match = _ATTACHMENT_STAMP.match(name)
    return match.group(1) if match else ""


def _dedupe_path(abs_path: Path) -> Path:
    """Return ``abs_path``, or the next ``-N`` variant when the file exists."""
    stem = abs_path.stem
    ext = abs_path.suffix
    counter = 2
    candidate = abs_path.parent / f"{stem}-{counter}{ext}"
    while candidate.exists():
        counter += 1
        candidate = abs_path.parent / f"{stem}-{counter}{ext}"
    return candidate


def _original_name(item: Path) -> str | None:
    """Return the ``original_name`` from a ``<file>.meta.json`` sidecar, or None."""
    sidecar = item.parent / f"{item.name}{_META_SUFFIX}"
    if not sidecar.is_file():
        return None
    try:
        meta = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name = meta.get("original_name") if isinstance(meta, dict) else None
    return name if isinstance(name, str) else None


def _atomic_write(abs_path: Path, data: bytes) -> None:
    """Write ``data`` to ``abs_path`` atomically, creating parent dirs in the root."""
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=abs_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, abs_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _check_frontmatter(content: str) -> None:
    """Validate a leading ``---`` YAML frontmatter block parses.

    A file that opens with a ``---`` fence but has no closing fence is a markdown
    horizontal rule, not frontmatter, so it is left alone.
    """
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            try:
                yaml.safe_load("\n".join(lines[1:index]))
            except yaml.YAMLError as exc:
                raise FilesError("invalid YAML frontmatter") from exc
            return


def _compile(query: str) -> re.Pattern[str]:
    """Compile ``query`` as a case-insensitive regex, or as a literal if it is not
    a valid regex."""
    try:
        return re.compile(query, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(query), re.IGNORECASE)


def _read_utf8(abs_path: Path) -> str | None:
    """Return the file text, or None when it is not UTF-8."""
    try:
        return abs_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, ValueError):
        return None


def _metadata(root: Root, abs_path: Path, stat: os.stat_result) -> dict[str, Any]:
    rel = root.name if root.is_file else f"{root.name}/{abs_path.relative_to(root.base).as_posix()}"
    return {
        "path": rel,
        "bytes": stat.st_size,
        "modified": _iso(stat.st_mtime),
        "note": "binary or non-UTF-8 file; content not shown",
    }


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, UTC).isoformat()


def _text(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


def _json(payload: Any) -> types.CallToolResult:
    body = json.dumps(payload, ensure_ascii=False)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=body)], structured_content={"result": payload}
    )


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )
