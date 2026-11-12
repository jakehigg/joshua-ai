"""``GET /api/v1/files/{root}/{path}``: the bytes of one file of the viewer.

An image answers with its own media type, so a page shows it inline. Every
other file answers with ``Content-Disposition: attachment``, so the browser
downloads it under its own name. A Markdown file downloads here too: a page
of the wiki renders through ``GET /api/v1/wiki``.

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


def file_url(root: str, rel: str) -> str:
    """The address that delivers one file: ``/api/v1/files/<root>/<rel>``."""
    from joshua_gateway.viewer_api import api_path  # this module is part of it

    return api_path(f"files/{root}/{quote(rel)}")


def _safe_filename(name: str) -> str:
    return name.replace('"', "").replace("\\", "").replace("\r", "").replace("\n", "")


def _download(abs_path: Path) -> Response:
    return Response(
        abs_path.read_bytes(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{_safe_filename(abs_path.name)}"'},
    )


def _not_found() -> JSONResponse:
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse({"error": "not found"}, status_code=404, headers=viewer._NO_STORE)


async def file(request: Request, person: Person) -> Response:
    """One file of a root the person reads: an image inline, anything else a
    download."""
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
    mime = IMAGE_MIME.get(abs_path.suffix.lower())
    if mime is not None:
        return Response(abs_path.read_bytes(), media_type=mime)
    return _download(abs_path)
