"""``GET /api/v1/files/{root}/{path}``: the bytes of one file of the viewer.

A file of a type a browser renders itself, an image or a PDF, answers with its
own media type and ``Content-Disposition: inline``, so a link to it opens the
browser's own viewer instead of starting a download. Every other file answers
with ``Content-Disposition: attachment``, so the browser downloads it under its
own name. A Markdown file downloads here too: a page of the wiki renders
through ``GET /api/v1/wiki``.

The type comes from the suffix of the stored name (``INLINE_MIME``), never from
the metadata file beside an attachment, whose content is untrusted. Every answer
carries ``X-Content-Type-Options: nosniff``, so the browser keeps to the type
this route named, and ``Content-Security-Policy: sandbox``, so a file that is
secretly HTML gets an opaque origin and runs nothing with the session.

``root`` is one of ``ROOTS``, the display roots of the viewer, and any other
name is 404. The path goes through the resolver of the files MCP
(``files_mcp.paths.resolve``), so a path that escapes its root, a hidden
entry, and a file that is not there are all 404.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from joshua_shared.config import Person
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from joshua_gateway.files_mcp.paths import PathError, resolve
from joshua_gateway.files_mcp.server import IMAGE_MIME

# The roots this route delivers: the display roots of ``viewer._roots_for``.
ROOTS = ("wiki", "shared", "attachments")

# The types this route serves inline, by the suffix of the stored name: the
# images of the files MCP, and PDF. ``channels`` names a stored attachment
# after the type it sniffed, so the suffix is the real type of the bytes.
INLINE_MIME = {**IMAGE_MIME, ".pdf": "application/pdf"}

# Every answer of this route carries these two. ``nosniff`` holds the browser
# to the type named here, and ``sandbox`` gives the file an opaque origin with
# no script, so a file that is secretly HTML does nothing with the session.
SAFE_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"}


def file_url(root: str, rel: str) -> str:
    """The address that delivers one file: ``/api/v1/files/<root>/<rel>``."""
    from joshua_gateway.viewer_api import api_path  # this module is part of it

    return api_path(f"files/{root}/{quote(rel)}")


def _safe_filename(name: str) -> str:
    return name.replace('"', "").replace("\\", "").replace("\r", "").replace("\n", "")


def _bytes(abs_path: Path, media_type: str, disposition: str) -> Response:
    name = _safe_filename(abs_path.name)
    return Response(
        abs_path.read_bytes(),
        media_type=media_type,
        headers={**SAFE_HEADERS, "Content-Disposition": f'{disposition}; filename="{name}"'},
    )


def _not_found() -> JSONResponse:
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse({"error": "not found"}, status_code=404, headers=viewer._NO_STORE)


async def file(request: Request, person: Person) -> Response:
    """One file of a root the person reads: an image or a PDF inline, anything
    else a download."""
    from joshua_gateway import viewer  # ``viewer`` imports this package

    root_name = request.path_params["root"]
    if root_name not in ROOTS:
        return _not_found()
    try:
        _, abs_path = resolve(
            f"{root_name}/{request.path_params['path']}", viewer._roots_for(person), write=False
        )
    except PathError:
        return _not_found()
    if not abs_path.is_file():
        return _not_found()
    mime = INLINE_MIME.get(abs_path.suffix.lower())
    if mime is not None:
        return _bytes(abs_path, mime, "inline")
    return _bytes(abs_path, "application/octet-stream", "attachment")
