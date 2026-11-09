"""The documents MCP server, hosted in-process by the gateway as the ``documents`` builtin.

A document is what Joshua works on or has produced for a member: a plan, a
report, a spec, a meal plan, an artifact. It has an owner, a status, and many
versions. It is not the wiki: the wiki is what Joshua knows, and a document's
body is never recall.

**The root.** ``/data/documents/`` holds ``<person>/`` for each member and
``shared/`` for every member. A tool path is ``documents/<owner>/<slug>.md``
(or ``.html``). ``.trash/<UTC timestamp>/`` holds a removed document. The root
is its own git repository, made on first use; each write is one commit by
Joshua with the message ``<tool>: <path>``, so the versions of a document are
its ``git log``.

**Access.** Members only. A member reads and writes their own folder and
``shared/``, and never the folder of another member. A guest, and a request
with no role, gets no tools: the list is empty and a call is refused. The role
and the person come from the request (``role_ctx``, ``person_ctx``), never from
a tool argument.

**Fields the server sets.** ``author``, ``created`` and ``updated`` are set
here, and ``promoted_to`` only by ``promote_document``. A caller sets
``title``, ``kind``, ``status``, ``format``, ``tags``, and ``summary``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import mcp_types as types
from joshua_shared import layout
from joshua_shared.log import get_logger
from mcp.server.lowlevel import Server

from joshua_gateway import viewer_docstore as docstore
from joshua_gateway.documents_mcp import store
from joshua_gateway.documents_mcp.store import DocumentError
from joshua_gateway.files_mcp.boundary import BoundaryError, check_boundary
from joshua_gateway.files_mcp.paths import MEMBER, UNKNOWN, PathError
from joshua_gateway.files_mcp.paths import roots as roots_for_role
from joshua_gateway.observability import identity_ctx, person_ctx, role_ctx

_logger = get_logger("documents_mcp")

# One document is at most this many bytes, frontmatter included.
MAX_BYTES = 256 * 1024
# A search returns at most this many documents.
MAX_SEARCH_RESULTS = 50
_TRASH_STAMP = "%Y%m%dT%H%M%SZ"

_PATH_HINT = "a document path is documents/<owner>/<slug>.md, where owner is your id or shared"
_NOT_YOURS = "that folder is not yours: you reach documents/<your id>/ and documents/shared/"
_MEMBERS_ONLY = "documents are for members"


def _now() -> datetime:
    """The current UTC time. A seam the tests override for a fixed clock."""
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


_PATH = {"type": "string", "description": "documents/<owner>/<slug>.md or .html"}

TOOLS = [
    types.Tool(
        name="list_documents",
        description=(
            "List documents: plans, reports, specs, artifacts, and notes that you work "
            "on for a person. By default, the documents of the person you talk to and "
            "the shared ones, and no archived ones. owner is your person id or shared. "
            "Each item has path, title, kind, status, tags, summary, author, updated, "
            "assignment, and open_comments."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "kind": {"type": "string", "enum": list(store.KINDS)},
                "status": {"type": "string", "enum": list(store.STATUSES)},
                "tag": {"type": "string"},
                "include_archived": {"type": "boolean", "default": False},
            },
        },
    ),
    types.Tool(
        name="read_document",
        description="Read one document: its frontmatter, its body, and its open comment count.",
        input_schema={"type": "object", "properties": {"path": _PATH}, "required": ["path"]},
    ),
    types.Tool(
        name="write_document",
        description=(
            "Create or replace one document at documents/<owner>/<slug>.md (or .html "
            "for an HTML artifact). Use a short slug of lower case words and hyphens. "
            "body is the text without frontmatter. A new document needs a title. On a "
            "replace, the fields you give change and the others stay. kind is plan, "
            "report, spec, artifact, or note (default note); status is draft, active, "
            "final, or archived (default draft). The server sets author, created, and "
            "updated. Each write is one version. Max 256 KB. A path under wiki/ is "
            "refused."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": _PATH,
                "body": {"type": "string"},
                "title": {"type": "string"},
                "kind": {"type": "string", "enum": list(store.KINDS)},
                "status": {"type": "string", "enum": list(store.STATUSES)},
                "tags": {"type": "array", "items": {"type": "string"}},
                "summary": {"type": "string"},
                "format": {"type": "string", "enum": list(store.FORMATS)},
                "message": {"type": "string", "description": "a note for the version"},
            },
            "required": ["path", "body"],
        },
    ),
    types.Tool(
        name="set_document_status",
        description="Set the status of one document: draft, active, final, or archived.",
        input_schema={
            "type": "object",
            "properties": {
                "path": _PATH,
                "status": {"type": "string", "enum": list(store.STATUSES)},
            },
            "required": ["path", "status"],
        },
    ),
    types.Tool(
        name="search_documents",
        description=(
            "Find documents whose title, tags, summary, or body hold the words, case "
            "insensitive, in your folder and the shared one. Returns path, title, kind, "
            "status, and a snippet. This is not search by meaning."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "include_archived": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="promote_document",
        description=(
            "Put the durable part of a finished document into the wiki: write markdown "
            "as the wiki page wiki_path (it replaces a page that is there), then archive "
            "the document with promoted_to set to wiki_path. The page follows the rules "
            "of write_file: it is reference, never a plan or a draft."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": _PATH,
                "wiki_path": {"type": "string", "description": "wiki/<folder>/<page>.md"},
                "markdown": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["path", "wiki_path", "markdown"],
        },
    ),
    types.Tool(
        name="trash_document",
        description="Move one document and its comments to the trash of the documents.",
        input_schema={"type": "object", "properties": {"path": _PATH}, "required": ["path"]},
    ),
    types.Tool(
        name="document_comments",
        description=(
            "Read the comments a person left on a document. Each comment names a line "
            "of the body (line_no from 1, line_text), or line_no 0 for the document as "
            "a whole; line_now is that line now, and stale is true when it changed after "
            "the comment. Open comments only, unless include_resolved."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": _PATH,
                "include_resolved": {"type": "boolean", "default": False},
            },
            "required": ["path"],
        },
    ),
    types.Tool(
        name="resolve_document_comment",
        description=(
            "Resolve one comment on a document after you applied it, with one line "
            "that says what you did."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": _PATH,
                "id": {"type": "integer"},
                "resolution": {"type": "string"},
            },
            "required": ["path", "id", "resolution"],
        },
    ),
]


@dataclass(frozen=True)
class Request:
    """Who calls: the person and whether the request may use the documents."""

    root_dir: Path
    person: str | None
    member: bool

    def owners(self) -> list[str]:
        """The folders this request reaches: its own (when it has one) and shared."""
        own = self.own()
        return [own, layout.DOCUMENTS_SHARED] if own else [layout.DOCUMENTS_SHARED]

    def own(self) -> str | None:
        person = self.person
        if person is None or person in (UNKNOWN, layout.DOCUMENTS_SHARED):
            return None
        try:
            layout.safe_segment(person)
        except ValueError:
            return None
        return person


@dataclass(frozen=True)
class Doc:
    """One resolved document path, whether the file exists or not."""

    owner: str
    slug: str
    ext: str
    abs_path: Path

    @property
    def rel(self) -> str:
        """The path in the documents repository: ``<owner>/<slug>.<ext>``."""
        return f"{self.owner}/{self.slug}.{self.ext}"

    @property
    def path(self) -> str:
        return f"{layout.DOCUMENTS}/{self.rel}"

    @property
    def fmt(self) -> str:
        return "html" if self.ext == "html" else "markdown"


def _role_from_config(person: str) -> str | None:
    from joshua_shared import config

    entry = config.load().person(person)
    return entry.role if entry is not None else None


def build_documents_server(
    root_dir: Path, *, role_for: Callable[[str], str | None] | None = None
) -> Server:
    """Build the documents ``Server`` for a data volume at ``root_dir``.

    ``role_for`` returns a person's role. It is the fallback for a request that
    carries a person but no role header, as in the files server.
    """
    roles = role_for or _role_from_config

    def request() -> Request:
        person = person_ctx.get()
        role = role_ctx.get()
        if role is None and person is not None and person != UNKNOWN:
            role = roles(person)
        return Request(root_dir=root_dir, person=person, member=role == MEMBER)

    async def on_list_tools(ctx, params):
        # The gateway lists the tools once at connect, with no request, for the
        # inventory. Every request of a person who is not a member sees none.
        if identity_ctx.get() == UNKNOWN and person_ctx.get() is None and role_ctx.get() is None:
            return types.ListToolsResult(tools=TOOLS)
        return types.ListToolsResult(tools=TOOLS if request().member else [])

    async def on_call_tool(ctx, params):
        args = params.arguments or {}
        try:
            handler = _HANDLERS.get(params.name)
            if handler is None:
                raise DocumentError(f"unknown tool: {params.name}")
            req = request()
            if not req.member:
                raise DocumentError(_MEMBERS_ONLY)
            return _json(handler(req, args))
        except (DocumentError, BoundaryError, PathError) as exc:
            return _error(exc.message)

    return Server("documents", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


# -- paths --------------------------------------------------------------------


def _resolve(req: Request, path: Any) -> Doc:
    """Resolve a tool path to a document path that this request may reach.

    The names follow the rules of ``viewer_docstore``, the one reader of the
    documents. Raises ``DocumentError`` for a path that is not
    ``documents/<owner>/<slug>.md`` or ``.html``, for a folder of another
    member, and for a path that a symlink takes out of the documents root. A
    message never holds an absolute path.
    """
    raw = str(path or "").strip()
    if not raw or "\x00" in raw or "\\" in raw or raw.startswith("/"):
        raise DocumentError(_PATH_HINT)
    parts = PurePosixPath(raw).parts
    if len(parts) != 3 or parts[0] != layout.DOCUMENTS:
        raise DocumentError(_PATH_HINT)
    owner, name = parts[1], parts[2]
    slug, dot, ext = name.rpartition(".")
    if not dot or ext not in docstore.FORMATS or not docstore.valid_owner(owner):
        raise DocumentError(_PATH_HINT)
    if not docstore.valid_slug(slug):
        raise DocumentError(f"{_PATH_HINT}; slug is letters, digits, '.', '_', and '-'")
    if owner not in req.owners():
        raise DocumentError(_NOT_YOURS)
    folder = layout.documents_owner_dir(owner, req.root_dir)
    abs_path = folder / name
    base = Path(os.path.realpath(layout.documents_root(req.root_dir)))
    real = Path(os.path.realpath(abs_path))
    if not real.is_relative_to(base) or folder.is_symlink() or abs_path.is_symlink():
        raise DocumentError("path escapes documents")
    return Doc(owner=owner, slug=slug, ext=ext, abs_path=abs_path)


def _existing(req: Request, path: Any) -> tuple[Doc, docstore.Document]:
    doc = _resolve(req, path)
    found = docstore.find(layout.documents_root(req.root_dir), doc.owner, doc.slug, req.owners())
    if found is None or found.ext != doc.ext:
        raise DocumentError("document not found")
    return doc, found


def _meta(found: docstore.Document) -> dict[str, Any]:
    """The frontmatter of ``found`` with each date and time as ISO text."""
    return {str(key): store.plain(value) for key, value in found.front.items()}


# -- git ----------------------------------------------------------------------


def _ensure_root(root_dir: Path) -> Path:
    """Make the documents root, its shared folder, and its git repository."""
    root = layout.documents_root(root_dir)
    layout.documents_owner_dir(layout.DOCUMENTS_SHARED, root_dir).mkdir(parents=True, exist_ok=True)
    try:
        from joshua_shared import wikigit

        wikigit.ensure_repo(root)
    except Exception as exc:  # noqa: BLE001 — git must never fail the write
        _logger.warning({"message": "documents git init failed", "error": str(exc)})
    return root


def _commit(root_dir: Path, paths: Iterable[Path], subject: str, message: Any = None) -> None:
    """Commit a write that already landed. A failure is a WARNING only."""
    note = store.one_line(message, "message", store.MAX_MESSAGE) if message else None
    text = f"{subject}\n\n{note}" if note else subject
    try:
        from joshua_shared import wikigit

        wikigit.commit(layout.documents_root(root_dir), list(paths), text)
    except Exception as exc:  # noqa: BLE001 — a commit must never fail the tool
        _logger.warning({"message": "documents commit failed", "error": str(exc)})


def _save(req: Request, doc: Doc, meta: dict[str, Any], body: str) -> int:
    data = store.join(meta, body).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise DocumentError("document exceeds 256 KB")
    _ensure_root(req.root_dir)
    store.atomic_write(doc.abs_path, data)
    return len(data)


# -- handlers -----------------------------------------------------------------


def _item(found: docstore.Document) -> dict[str, Any]:
    return {
        "path": found.tool_path,
        "title": found.title,
        "kind": found.kind,
        "status": found.status,
        "tags": found.tags,
        "summary": found.summary,
        "author": found.author or None,
        "updated": found.updated,
        "assignment": found.assignment or None,
        "open_comments": docstore.open_count(found),
    }


def _documents(req: Request, owners: list[str]) -> list[docstore.Document]:
    return docstore.list_documents(layout.documents_root(req.root_dir), owners)


def _list_documents(req: Request, args: dict[str, Any]) -> list[dict[str, Any]]:
    owner = args.get("owner")
    if owner:
        if owner not in req.owners():
            raise DocumentError(_NOT_YOURS)
        owners = [owner]
    else:
        owners = req.owners()
    kind = store.choice(args.get("kind"), "kind", store.KINDS)
    status = store.choice(args.get("status"), "status", store.STATUSES)
    tag = args.get("tag")
    include_archived = bool(args.get("include_archived")) or status == "archived"

    items = []
    for found in _documents(req, owners):
        if kind and found.kind != kind:
            continue
        if status and found.status != status:
            continue
        if not include_archived and found.status == "archived":
            continue
        if tag and tag not in found.tags:
            continue
        items.append(_item(found))
    return items


def _read_document(req: Request, args: dict[str, Any]) -> dict[str, Any]:
    doc, found = _existing(req, args.get("path"))
    return {
        "path": doc.path,
        "frontmatter": _meta(found),
        "body": found.body,
        "open_comments": docstore.open_count(found),
    }


def _write_document(req: Request, args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path") or "")
    check_boundary(path, "", tool="write_document")
    doc = _resolve(req, path)
    body = args.get("body")
    if not isinstance(body, str):
        raise DocumentError("body is the text of the document")
    if store.has_frontmatter(body):
        raise DocumentError("body has no frontmatter: give title, kind, status, and tags as fields")
    fmt = store.choice(args.get("format"), "format", store.FORMATS)
    if fmt is not None and fmt != doc.fmt:
        raise DocumentError(f"format {fmt} does not match the file name {doc.abs_path.name}")
    for other in docstore.FORMATS:
        sibling = doc.abs_path.with_name(f"{doc.slug}.{other}")
        if other != doc.ext and sibling.exists():
            raise DocumentError(
                f"documents/{doc.owner}/{sibling.name} exists: one slug, one document"
            )

    created = not doc.abs_path.is_file()
    if created:
        meta: dict[str, Any] = {"kind": "note", "status": "draft", "tags": []}
        title = store.one_line(args.get("title"), "title", store.MAX_TITLE, required=True)
    else:
        _, found = _existing(req, path)
        meta = _meta(found)
        title = store.one_line(args.get("title"), "title", store.MAX_TITLE)

    updates = {
        "title": title,
        "kind": store.choice(args.get("kind"), "kind", store.KINDS),
        "status": store.choice(args.get("status"), "status", store.STATUSES),
        "tags": store.tags_of(args.get("tags")),
        "summary": store.one_line(args.get("summary"), "summary", store.MAX_SUMMARY),
    }
    meta.update({key: value for key, value in updates.items() if value is not None})
    now = _stamp(_now())
    meta["format"] = doc.fmt
    meta["author"] = meta.get("author") or req.own() or UNKNOWN
    meta["created"] = meta.get("created") or now
    meta["updated"] = now

    size = _save(req, doc, meta, body)
    _commit(req.root_dir, [doc.abs_path], f"write_document: {doc.rel}", args.get("message"))
    return {
        "path": doc.path,
        "created": created,
        "bytes": size,
        "kind": meta.get("kind", "note"),
        "status": meta.get("status", "draft"),
    }


def _set_document_status(req: Request, args: dict[str, Any]) -> dict[str, Any]:
    status = store.choice(args.get("status"), "status", store.STATUSES)
    if status is None:
        raise DocumentError(f"status is one of {', '.join(store.STATUSES)}")
    doc, found = _existing(req, args.get("path"))
    meta = _meta(found)
    meta["status"] = status
    meta["updated"] = _stamp(_now())
    _save(req, doc, meta, found.body)
    _commit(req.root_dir, [doc.abs_path], f"set_document_status: {doc.rel}")
    return {"path": doc.path, "status": status}


def _search_documents(req: Request, args: dict[str, Any]) -> list[dict[str, Any]]:
    query = str(args.get("query") or "").strip().lower()
    if not query:
        raise DocumentError("query is the words to find")
    include_archived = bool(args.get("include_archived"))
    hits = []
    for found in _documents(req, req.owners()):
        if not include_archived and found.status == "archived":
            continue
        summary = " ".join(str(found.front.get("summary") or "").split())
        fields = [found.title, " ".join(found.tags), summary]
        snippet = next((text for text in fields if query in text.lower()), None)
        if snippet is None:
            snippet = next(
                (line.strip() for line in found.body_lines if query in line.lower()), None
            )
        if snippet is None:
            continue
        hits.append(
            {
                "path": found.tool_path,
                "title": found.title,
                "kind": found.kind,
                "status": found.status,
                "snippet": snippet[:200],
            }
        )
        if len(hits) >= MAX_SEARCH_RESULTS:
            break
    return hits


def _promote_document(req: Request, args: dict[str, Any]) -> dict[str, Any]:
    """Write the wiki page through ``write_wiki_page``, then archive the document.

    The wiki write runs first and is its own commit in the wiki repository, so
    a refused page leaves the document as it was. The archive is the second
    commit, in the documents repository.
    """
    from joshua_gateway.files_mcp.server import FilesError, write_wiki_page

    doc, found = _existing(req, args.get("path"))
    wiki_path = str(args.get("wiki_path") or "").strip()
    if not wiki_path.startswith("wiki/"):
        raise DocumentError("wiki_path is a page under wiki/, such as wiki/recipes/soup.md")
    markdown = args.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise DocumentError("markdown is the text of the wiki page")
    wiki_roots = roots_for_role(req.root_dir, role=MEMBER)
    try:
        written = write_wiki_page(
            wiki_roots, wiki_path, markdown, "overwrite", req.root_dir, tool="promote_document"
        )
    except FilesError as exc:
        raise DocumentError(exc.message) from exc

    meta = _meta(found)
    meta["status"] = "archived"
    meta["promoted_to"] = wiki_path
    meta["updated"] = _stamp(_now())
    _save(req, doc, meta, found.body)
    _commit(req.root_dir, [doc.abs_path], f"promote_document: {doc.rel}", args.get("message"))
    return {
        "path": doc.path,
        "wiki_path": written["path"],
        "bytes": written["bytes"],
        "status": "archived",
    }


def _trash_document(req: Request, args: dict[str, Any]) -> dict[str, Any]:
    doc, found = _existing(req, args.get("path"))
    stamp = _now().astimezone(UTC).strftime(_TRASH_STAMP)
    folder = layout.documents_trash_dir(stamp, req.root_dir) / doc.owner
    folder.mkdir(parents=True, exist_ok=True)
    comments_file = docstore.comments_path(found)
    moved = [doc.abs_path]
    if comments_file.exists():
        moved.append(comments_file)
    for source in moved:
        os.replace(source, _free(folder / source.name))
    _ensure_root(req.root_dir)
    # The owner's folder, not the two files: git refuses a path it never knew,
    # and a comments file can be new since the last commit.
    _commit(req.root_dir, [doc.abs_path.parent], f"trash_document: {doc.rel}")
    return {"path": doc.path, "trashed": True, "comments_moved": len(moved) == 2}


def _free(target: Path) -> Path:
    """``target``, or ``<name>-2``, ``-3``, ... when a file is there already."""
    if not target.exists():
        return target
    stem = target.name.split(".", 1)[0]
    rest = target.name[len(stem) :]
    counter = 2
    while (target.parent / f"{stem}-{counter}{rest}").exists():
        counter += 1
    return target.parent / f"{stem}-{counter}{rest}"


def _comment_view(comment: docstore.DocComment, found: docstore.Document) -> dict[str, Any]:
    return {
        "id": comment.id,
        "line_no": comment.line_no,
        "line_text": comment.line_text,
        "line_now": found.line(comment.line_no),
        "body": comment.body,
        "author": comment.author or None,
        "created_at": comment.created_at,
        "resolved_at": comment.resolved_at,
        "resolution": comment.resolution,
        "stale": comment.stale,
    }


def _document_comments(req: Request, args: dict[str, Any]) -> dict[str, Any]:
    doc, found = _existing(req, args.get("path"))
    include_resolved = bool(args.get("include_resolved"))
    comments = docstore.read_comments(found)
    shown = [c for c in comments if include_resolved or c.resolved_at is None]
    still_open = [c for c in comments if c.resolved_at is None]
    return {
        "path": doc.path,
        "comments": [_comment_view(c, found) for c in shown],
        "open": len(still_open),
        "stale": sum(1 for c in still_open if c.stale),
    }


def _resolve_document_comment(req: Request, args: dict[str, Any]) -> dict[str, Any]:
    doc, found = _existing(req, args.get("path"))
    resolution = store.one_line(
        args.get("resolution"), "resolution", store.MAX_RESOLUTION, required=True
    )
    wanted = str(args.get("id"))
    comments = docstore.read_comments(found)
    comment = next((c for c in comments if str(c.id) == wanted), None)
    if comment is None:
        raise DocumentError(f"no comment {wanted} on this document")
    if comment.resolved_at is not None:
        raise DocumentError(f"comment {wanted} is resolved already")
    comments_file = docstore.comments_path(found)
    store.set_resolution(comments_file, comment.id, resolution, _stamp(_now()))
    _ensure_root(req.root_dir)
    _commit(req.root_dir, [comments_file], f"resolve_document_comment: {doc.rel}")
    still_open = sum(1 for c in comments if c.resolved_at is None) - 1
    return {"path": doc.path, "id": comment.id, "open": still_open}


_HANDLERS: dict[str, Callable[[Request, dict[str, Any]], Any]] = {
    "list_documents": _list_documents,
    "read_document": _read_document,
    "write_document": _write_document,
    "set_document_status": _set_document_status,
    "search_documents": _search_documents,
    "promote_document": _promote_document,
    "trash_document": _trash_document,
    "document_comments": _document_comments,
    "resolve_document_comment": _resolve_document_comment,
}


def _json(payload: Any) -> types.CallToolResult:
    body = json.dumps(payload, ensure_ascii=False)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=body)], structured_content={"result": payload}
    )


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )
