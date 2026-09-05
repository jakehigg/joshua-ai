"""The files MCP server, hosted in-process by the gateway as the ``files`` builtin.

It lets the agent list, read, search, write, and rename a page in the one wiki
(``wiki/``, a member writes, a guest reads), add an entry to Joshua's own
journal there (``write_journal_entry``), and read a person's attachments and
terminal outbox (``people/<person>/``) and the shared attachments (``shared/``).
It is the agent's only file interface. It replaces the SDK
``Read``/``Write``/``Edit`` tools, so the agent reaches exactly these paths and
nothing else.

**One corpus.** The wiki is what Joshua knows: pages, its own journal, and its
profile of each person. ``people/<person>/`` and ``shared/`` hold what another
container wrote — an attachment, a terminal outbox — and are read-only here. A
member writes the wiki. A guest writes nothing, and a request with no role
writes nothing.

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
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

import mcp_types as types
import yaml
from joshua_shared.layout import is_hidden, journal_entry_path, journal_root
from joshua_shared.log import get_logger
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

_logger = get_logger("files_mcp")

# The data volume, fixed at /data in every container; overridable for tests.
DATA_ENV = "JOSHUA_DATA_DIR"
DEFAULT_DATA_ROOT = "/data"

# One write, and one non-image attachment read, is at most this many bytes.
MAX_BYTES = 256 * 1024

# A PDF read extracts text from at most this many pages, and never more than
# ``MAX_BYTES`` of text. A longer PDF is capped and the header says so.
PDF_MAX_PAGES = 20

# Characters allowed in a stored filename stem (the same rule as channels'
# ``safe_filename``). Everything else becomes ``_``.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")

# The metadata file a channels-stored attachment carries: ``<file>.meta.json``.
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
    """The current UTC time. A seam the tests override for a fixed journal clock."""
    return datetime.now(UTC)


TOOLS = [
    types.Tool(
        name="list_files",
        description=(
            "List files under one root. wiki is the one wiki that everyone uses; "
            "wiki/joshua-docs/ holds Joshua's own documentation, wiki/journal/ holds "
            "Joshua's own journal, and wiki/people/ holds Joshua's profile of "
            "each person. people holds a person's own attachments and terminal "
            "history, one directory per person id; shared holds the group's "
            "shared attachments. Pass subpath to go deeper, such as root people "
            "and subpath <person-id>/attachments. Returns [{path, bytes, "
            "modified}]; each path is root-relative and accepted by read_file. "
            "An attachment adds original_name when the sender's filename is "
            "known."
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
            "Write one .md file under wiki/. mode create fails if the file "
            "exists; overwrite replaces it; append adds to it. Max 256 KB. A "
            "path under wiki/journal/ is refused: the journal has one writer, "
            "write_journal_entry."
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
            "Rename one file in place under wiki/. new_name is a bare filename; "
            "the extension must not change."
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
    types.Tool(
        name="write_journal_entry",
        description=(
            "Add one entry to Joshua's own journal, at wiki/journal/. There "
            "is one journal and it is Joshua's: a person is named in an entry, "
            "never the owner of one. One folder holds a day; an entry is one "
            "page in it, member only. Write it in the third person, as "
            "Joshua's own record of what happened, not a message to a person. "
            "Add an entry only for a life update worth remembering in a month "
            "— a visit, a plan, a change — never for a question, for small "
            "talk, or for a request to operate a tool. A person who asks "
            "Joshua to write in a journal, blog, or notes of their own on "
            "another service gets it there; this journal does not copy it. "
            "One entry per event: to correct one already written today, send "
            "the same slug again rather than adding a second entry. Name the "
            "people the entry is about in people; leave it empty for an entry "
            "about nobody in particular. slug is a short filename word, such "
            "as alex-breakfast; the server places it in the right day's "
            "folder. date defaults to today and is YYYY-MM-DD. An entry that "
            "already exists at that day and slug is overwritten."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$"},
                "markdown": {"type": "string"},
                "people": {"type": "array", "items": {"type": "string"}, "default": []},
                "date": {"type": "string"},
            },
            "required": ["slug", "markdown"],
        },
    ),
]


def _role_from_config(person: str) -> str | None:
    """The configured role of ``person``, or None when unknown."""
    from joshua_shared import config

    entry = config.load().person(person)
    return entry.role if entry is not None else None


def build_files_server(
    root_dir: Path,
    *,
    timezone: str = "UTC",
    role_for: Callable[[str], str | None] | None = None,
) -> Server:
    """Build the files ``Server`` for a data volume at ``root_dir``.

    ``timezone`` is the configured timezone; the server stamps a journal entry
    name in it. ``role_for`` returns a person's role, and it is the fallback for
    a request that carries a person but no role header.
    """
    tz = ZoneInfo(timezone)
    roles = role_for or _role_from_config

    def roots_for_request() -> dict[str, Root]:
        """The root set of this request, from its role alone.

        The corpus is shared, so the role decides what a request reads and
        whether it writes. ``role_ctx`` holds what core asserted. A core that
        sends no role header falls back to the role of the person it names; a
        request with neither reads and writes nothing.
        """
        person = person_ctx.get()
        role = role_ctx.get()
        if role is None:
            if person is not None and person != UNKNOWN:
                role = roles(person)
        return roots_for_role(root_dir, role=role or "")

    async def on_list_tools(ctx, params):
        return types.ListToolsResult(tools=TOOLS)

    async def on_call_tool(ctx, params):
        args = params.arguments or {}
        try:
            handler = _HANDLERS.get(params.name)
            if handler is None:
                raise FilesError(f"unknown tool: {params.name}")
            return handler(roots_for_request(), args, tz, root_dir)
        except (PathError, FilesError) as exc:
            return _error(exc.message)

    return Server("files", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


# -- tool handlers ----------------------------------------------------------


def _list_files(
    roots: dict[str, Root], args: dict[str, Any], tz: ZoneInfo, root_dir: Path
) -> types.CallToolResult:
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


def _read_file(
    roots: dict[str, Root], args: dict[str, Any], tz: ZoneInfo, root_dir: Path
) -> types.CallToolResult:
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


def _in_journal(abs_path: Path, root_dir: Path) -> bool:
    """True when ``abs_path`` is inside ``wiki/journal/``.

    The journal has one writer, ``write_journal_entry``, so that every entry
    lands in the right day folder with the frontmatter the index reads, and so
    that the nightly page cannot be overwritten by a free write.
    """
    return abs_path == journal_root(root_dir) or journal_root(root_dir) in abs_path.parents


def _write_file(
    roots: dict[str, Root], args: dict[str, Any], tz: ZoneInfo, root_dir: Path
) -> types.CallToolResult:
    content = args.get("content", "")
    mode = args.get("mode", "create")
    if mode not in ("create", "overwrite", "append"):
        raise FilesError("mode must be create, overwrite, or append")
    path = args.get("path", "")
    root, abs_path = resolve(path, roots, write=True)
    if _in_journal(abs_path, root_dir):
        raise FilesError("the journal is written with write_journal_entry")

    _check_frontmatter(content)
    data = content.encode("utf-8")
    if mode == "create" and abs_path.exists():
        raise FilesError("file exists")
    if mode == "append" and abs_path.exists():
        data = abs_path.read_bytes() + data
    if len(data) > MAX_BYTES:
        raise FilesError("file exceeds 256 KB")

    _atomic_write(abs_path, data)
    _commit_wiki(root, [abs_path], f"write_file: {_wiki_rel(root, abs_path)}")
    return _json({"path": path, "bytes": len(data)})


def _rename_file(
    roots: dict[str, Root], args: dict[str, Any], tz: ZoneInfo, root_dir: Path
) -> types.CallToolResult:
    rel = args.get("path", "")
    root, src = resolve(rel, roots, write=False)
    if root.name != "wiki":
        raise FilesError(f"cannot rename in {root.name}")
    if not root.can_write:
        raise FilesError(f"{root.name} is read-only for a guest")
    if _in_journal(src, root_dir):
        raise FilesError("a journal entry cannot be renamed")
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

    final = f"{stem}{src.suffix}"
    dest = src.parent / final
    if dest == src:
        raise FilesError("new_name matches the current name")
    if dest.exists():
        raise FilesError("target exists")

    os.rename(src, dest)
    _commit_wiki(
        root,
        [src, dest],
        f"rename_file: {_wiki_rel(root, src)} -> {_wiki_rel(root, dest)}",
    )
    return _json({"path": _rel_path(root, dest), "renamed_from": _rel_path(root, src)})


def _search_files(
    roots: dict[str, Root], args: dict[str, Any], tz: ZoneInfo, root_dir: Path
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


def _write_journal_entry(
    roots: dict[str, Root], args: dict[str, Any], tz: ZoneInfo, root_dir: Path
) -> types.CallToolResult:
    """Add one entry to Joshua's own journal, at ``wiki/journal/YYYY/MM/DD/<slug>.md``.

    Member only; a guest gets the same denial as a forbidden wiki write. The day
    comes from ``date`` (``YYYY-MM-DD``) or from today in the configured
    timezone. ``slug`` is validated by ``layout.journal_entry_path``, which
    raises for a bad one. An entry that already exists at that day and slug is
    overwritten, and the result says so.
    """
    root = roots.get("wiki")
    if root is None or not root.can_write:
        raise FilesError("wiki is read-only")

    people = args.get("people")
    if people is None:
        people = []
    if not isinstance(people, list) or not all(isinstance(item, str) for item in people):
        raise FilesError("people must be a list of person ids")

    date_arg = args.get("date")
    if date_arg:
        try:
            day = date.fromisoformat(str(date_arg))
        except ValueError as exc:
            raise FilesError("date must be YYYY-MM-DD") from exc
    else:
        day = _now().astimezone(tz).date()

    try:
        abs_path = journal_entry_path(day, str(args.get("slug", "")), root=root_dir)
    except ValueError as exc:
        raise FilesError(str(exc)) from exc

    overwritten = abs_path.exists()
    block = yaml.safe_dump(
        {"date": day.isoformat(), "people": people, "source": "agent"}, sort_keys=False
    ).strip()
    data = f"---\n{block}\n---\n\n{args.get('markdown', '')}".encode()
    if len(data) > MAX_BYTES:
        raise FilesError("file exceeds 256 KB")

    _atomic_write(abs_path, data)
    _commit_wiki(root, [abs_path], f"write_journal_entry: {_wiki_rel(root, abs_path)}")
    return _json(
        {"path": _rel_path(root, abs_path), "bytes": len(data), "overwritten": overwritten}
    )


_HANDLERS = {
    "list_files": _list_files,
    "read_file": _read_file,
    "write_file": _write_file,
    "rename_file": _rename_file,
    "search_files": _search_files,
    "write_journal_entry": _write_journal_entry,
}


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
    which root it came from. ``people/alex/attachments/2026/08/a.jpg`` gives
    ``attachments``; ``people/alex/cli/outbox.jsonl`` gives ``cli``.
    """
    parts = PurePosixPath(path.strip()).parts
    if len(parts) >= 3 and parts[0] == "people":
        return parts[2]
    return None


def _rel_path(root: Root, abs_path: Path) -> str:
    """Return the root-relative path of ``abs_path`` for a listing or a result."""
    return f"{root.name}/{abs_path.relative_to(root.base).as_posix()}"


def _wiki_rel(root: Root, abs_path: Path) -> str:
    """``abs_path`` relative to the wiki repository itself, for a commit message."""
    return abs_path.relative_to(root.base).as_posix()


def _commit_wiki(root: Root, paths: Iterable[Path], message: str) -> None:
    """Commit a write that landed in the wiki, when ``wiki.git`` is enabled.

    Runs after the write already succeeded, so this never changes the tool's
    result: any failure here, including a monkeypatched ``wikigit.commit``
    that raises, is a WARNING only.
    """
    if root.name != "wiki":
        return
    try:
        from joshua_shared import config, wikigit

        if not wikigit.is_enabled(config.load()):
            return
        wikigit.commit(root.base, list(paths), message)
    except Exception as exc:  # noqa: BLE001 — a commit must never fail the tool
        _logger.warning({"message": "wiki commit failed", "error": str(exc)})


def _sanitize_stem(raw: str) -> str:
    """Return a safe filename stem (channels' ``safe_filename`` stem rule)."""
    stem = posixpath.basename(str(raw).replace("\\", "/")).strip()
    return _UNSAFE_CHARS.sub("_", stem)[:48].strip("._-")


def _original_name(item: Path) -> str | None:
    """Return the ``original_name`` from a ``<file>.meta.json`` metadata, or None."""
    metadata = item.parent / f"{item.name}{_META_SUFFIX}"
    if not metadata.is_file():
        return None
    try:
        meta = json.loads(metadata.read_text(encoding="utf-8"))
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
    return {
        "path": _rel_path(root, abs_path),
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
