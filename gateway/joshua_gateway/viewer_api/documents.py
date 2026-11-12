"""The JSON routes of the documents: the list, one document, and its comments.

- ``GET /api/v1/documents?status=&kind=`` gives the documents of the member
  who signed in and the shared ones, one status at a time, with how many
  documents each status holds and the kinds of the chips.
- ``GET /api/v1/documents/{owner}/{slug}?rev=<sha>`` gives one document: its
  frontmatter, its blocks, every line of its body, its comments, and its
  versions. ``?rev=`` gives an older version, which is read-only.
- ``POST /api/v1/documents/{owner}/{slug}/comments`` adds one comment of the
  person on a line of the body.
- ``POST /api/v1/documents/{owner}/{slug}/comments/{cid}/delete`` removes
  their own open comment.

A member reads their own folder and ``shared/``, and no other member's
folder; a guest gets no documents at all. ``viewer._document`` calls
``viewer_docstore.find``, which enforces that, so a document of another
member answers 404, never 403.

A comment names a line of the body, from 1 at the first line after the
closing ``---`` of the frontmatter. Line 0 is the document as a whole, and
its text is the ``title:`` line. The blocks keep the line numbers that
``viewer_blocks`` computes, because a comment anchors to a line, and each
write of a comment is one commit of the documents repository.

The answers hold data: the React app renders the blocks, the gutter and the
list (``src/blocks/``, ``src/comments/``, ``src/pages/Documents.tsx`` and
``src/pages/Document.tsx``). ``GET /documents/{owner}/{slug}.json`` and
``GET /documents/{owner}/{slug}/artifact`` stay in ``viewer``: the chat reads
the first for the head of an artifact card and frames the second for an HTML
document.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from joshua_shared.config import Person
from joshua_shared.log import get_logger
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway import viewer_blocks as vb
from joshua_gateway import viewer_comments as comments_mod
from joshua_gateway import viewer_docstore as docstore
from joshua_gateway import viewer_documents as documents_mod
from joshua_gateway import viewer_report as rp
from joshua_gateway.viewer_api.edit import (
    NOT_JSON,
    _answer,
    _bad,
    _csrf_token,
    _csrf_valid,
    _field,
    _fields,
    _origin_ok,
    _refused,
)

logger = get_logger("viewer")


def comments_scope(url: str) -> str:
    """The CSRF scope of the comments of the document at ``url``."""
    return f"comments:{url}"


def _not_found() -> JSONResponse:
    return _answer({"error": "not found"}, 404)


def _json_value(value: Any) -> Any:
    """One frontmatter value as JSON: a time as text, a nest as a nest."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _frontmatter(doc: docstore.Document) -> dict[str, Any]:
    """The frontmatter of ``doc`` as JSON, in the order of the file."""
    return {str(key): _json_value(value) for key, value in doc.front.items()}


def _comment(comment: docstore.DocComment, doc: docstore.Document) -> dict[str, Any]:
    """One comment: where it is, whether its line changed, and who wrote it."""
    return {
        "id": str(comment.id),
        "line_no": comment.line_no,
        "line_text": comment.line_text,
        "line_now": doc.line(comment.line_no),
        "stale": comment.stale,
        "body": comment.body,
        "author": comment.author,
        "created_at": comment.created_at,
        "resolved_at": comment.resolved_at,
        "resolution": comment.resolution,
    }


def _comments(doc: docstore.Document) -> list[dict[str, Any]]:
    return [_comment(c, doc) for c in docstore.read_comments(doc)]


# -- the list ---------------------------------------------------------------


def _row(doc: docstore.Document, *, person_id: str, open_comments: int) -> dict[str, Any]:
    return {
        "path": doc.tool_path,
        "url": doc.url,
        "title": doc.title,
        "kind": doc.kind,
        "status": doc.status,
        "owner": doc.owner,
        "owner_label": documents_mod.owner_label(doc.owner, person_id),
        "summary": doc.summary,
        "updated": doc.updated,
        "open_comments": open_comments,
    }


async def documents(request: Request, person: Person) -> JSONResponse:
    """The documents of the person and the shared ones, of one status.

    ``counts`` says how many documents each status holds, so the tabs of the
    page name a number, and it follows the kind filter, because a tab keeps
    the chip of the kind. ``kinds`` is every kind of the chips: the known
    ones, then any other kind a document names. Only a document of the
    status that shows counts its open comments, as the page does. A guest is
    404, and with no documents folder the list is empty and nothing is
    created.
    """
    if person.role != "member":
        return _not_found()
    status = documents_mod.parse_status(request.query_params.get("status"))
    kind = documents_mod.parse_kind(request.query_params.get("kind"))
    base = docstore.root()
    docs = docstore.list_documents(base, docstore.owners(person.id)) if base.is_dir() else []
    of_kind = [d for d in docs if kind is None or d.kind == kind]
    counts = {key: sum(1 for d in of_kind if d.status == key) for key, _, _ in documents_mod.PILLS}
    shown = [d for d in of_kind if d.status == status]
    kinds = list(dict.fromkeys([*docstore.KINDS, *sorted({d.kind for d in docs})]))
    return _answer(
        {
            "status": status,
            "kind": kind,
            "counts": counts,
            "kinds": kinds,
            "documents": [
                _row(d, person_id=person.id, open_comments=docstore.open_count(d)) for d in shown
            ],
        }
    )


# -- one document -----------------------------------------------------------


def _blocks(doc: docstore.Document) -> list[dict[str, Any]]:
    """The blocks of a Markdown document, each with its lines of the body.

    An HTML document has no block: its body renders in the frame of the
    artifact route.
    """
    if doc.format == "html":
        return []
    lines = doc.body_lines
    return [
        {
            "id": f"L{block.first}",
            "kind": block.kind,
            "depth": min(block.depth, vb.MAX_DEPTH),
            "markdown": vb.block_markdown(lines, block),
            "first_line": block.first,
            "last_line": block.last,
        }
        for block in vb.blocks(doc.body)
    ]


def _section(key: str, section: rp.Section) -> dict[str, Any]:
    return {
        "key": key,
        "title": section.title,
        "body": section.body,
        "line": section.line,
        "start": section.start,
        "end": section.end,
    }


def _report(doc: docstore.Document) -> dict[str, Any] | None:
    """The sections of a document that has the headings of a report, else None."""
    if doc.format == "html":
        return None
    report = rp.parse(doc.body)
    if not report.structured:
        return None
    return {
        "title": report.title,
        "lede": report.lede,
        "generated": report.generated,
        "preamble_line": report.preamble_line,
        "first_section": report.first_section,
        "sections": [_section(key, part) for key, part in report.sections.items()],
        "extra": [_section("", part) for part in report.extra],
    }


async def document(request: Request, person: Person) -> JSONResponse:
    """One document, as the page of the React app reads it.

    ``lines`` are the lines of the body, which a comment counts from 1, and
    ``preamble`` the lines of the frontmatter, which the Source fold shows
    first with no number and no "+".

    ``?rev=<sha>`` gives the document at that commit of its file: read-only,
    with no comment and no token, so the page offers no "+". A document of
    another member, one that is not there, and a ``rev`` that is no version
    of the file are each 404.
    """
    from joshua_gateway import viewer  # ``viewer`` imports this package

    doc = viewer._document(request, person)
    if doc is None:
        return _not_found()
    base = docstore.root()
    versions = docstore.versions(base, doc)
    version = None
    shown = doc
    rev = request.query_params.get("rev")
    if rev:
        found = docstore.at_version(base, doc, rev)
        if found is None:
            return _not_found()
        version, shown = found
    old = version is not None
    artifact = f"{doc.url}/artifact" if shown.format == "html" else None
    if artifact is not None and version is not None:
        artifact = f"{artifact}?rev={version.sha}"
    return _answer(
        {
            "path": shown.tool_path,
            "url": doc.url,
            "title": shown.title,
            "kind": shown.kind,
            "status": shown.status,
            "format": shown.format,
            "summary": shown.summary,
            "updated": shown.updated,
            "owner": shown.owner,
            "owner_label": documents_mod.owner_label(shown.owner, person.id),
            "frontmatter": _frontmatter(shown),
            "artifact_url": artifact,
            "blocks": _blocks(shown),
            "report": _report(shown),
            "lines": shown.body_lines,
            "preamble": shown.lines[: shown.offset],
            "comments": [] if old else _comments(doc),
            "versions": [{"sha": v.sha, "when": v.when, "message": v.subject} for v in versions],
            "version": None if version is None else {"sha": version.sha, "when": version.when},
            "csrf": None if old else _csrf_token(person.id, comments_scope(doc.url)),
        }
    )


# -- the comments -----------------------------------------------------------


def _line_no(fields: dict[str, Any]) -> int:
    """The ``line_no`` of the body, or -1 when it is not a number."""
    raw = fields.get("line_no")
    if isinstance(raw, bool):
        return -1
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1


async def add_comment(request: Request, person: Person) -> JSONResponse:
    """Add one comment of the person on a line of a document, in one commit.

    The checks are those of every write: the document the person may read,
    the CSRF token of the scope of the page, and the ``Origin`` header. A
    line that is not in the document, an empty comment, and one over
    ``MAX_COMMENT`` characters are each 400 with the message the page shows.
    """
    from joshua_gateway import viewer  # ``viewer`` imports this package

    doc = viewer._document(request, person)
    if doc is None:
        return _not_found()
    fields = await _fields(request)
    if fields is None:
        return _bad(NOT_JSON)
    if not _csrf_valid(person.id, comments_scope(doc.url), _field(fields, "csrf")):
        return _refused(403)
    if not _origin_ok(request):
        return _refused(403)
    line_no = _line_no(fields)
    anchor = doc.line(line_no)
    if anchor is None:
        return _bad("The line is not in the document.")
    body = _field(fields, "body").strip()
    if not body:
        return _bad("The comment is empty.")
    if len(body) > comments_mod.MAX_COMMENT:
        return _bad(f"The comment exceeds {comments_mod.MAX_COMMENT} characters.")
    docstore.add_comment(
        docstore.root(),
        doc,
        author=person.id,
        line_no=line_no,
        line_text=_field(fields, "line_text") or anchor,
        body=body,
        now=viewer._now(UTC),
    )
    logger.info({"message": "viewer comment", "kind": "document", "delete": False})
    return _answer({"ok": True, "anchor": f"L{line_no}", "comments": _comments(doc)})


async def delete_comment(request: Request, person: Person) -> JSONResponse:
    """Remove one open comment of the person, in one commit. A comment that
    is not their own open comment is 404."""
    from joshua_gateway import viewer  # ``viewer`` imports this package

    doc = viewer._document(request, person)
    if doc is None:
        return _not_found()
    fields = await _fields(request)
    if fields is None:
        return _bad(NOT_JSON)
    if not _csrf_valid(person.id, comments_scope(doc.url), _field(fields, "csrf")):
        return _refused(403)
    if not _origin_ok(request):
        return _refused(403)
    cid = str(request.path_params["cid"])
    if not docstore.delete_comment(docstore.root(), doc, author=person.id, comment_id=cid):
        return _not_found()
    logger.info({"message": "viewer comment", "kind": "document", "delete": True})
    return _answer({"ok": True, "comments": _comments(doc)})
