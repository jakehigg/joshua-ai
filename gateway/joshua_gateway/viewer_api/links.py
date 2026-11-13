"""The JSON routes of the links: the list of the sidebar, and its editor.

- ``GET /api/v1/links`` gives the saved links, the names of the built-in
  icons, whether the person of the request may edit them, and the CSRF token
  of the write. Only a member gets ``can_edit`` true and a token.
- ``POST /api/v1/links`` replaces the whole list with the rows of the body.

This route is the one source of the list: the ``/links`` page and the links
group of the sidebar (``shell/LinksNav.tsx``) both read it, so
``GET /api/v1/session`` carries no links.

The list lives in one file, ``wiki/.viewer/links.yaml``
(``viewer_links.links_path``). The dot directory keeps it out of the wiki
tree, the search and the memory index, and the wiki's git repository records
each change.

A URL must be an ``http`` or an ``https`` URL, and an icon must be a built-in
one (``viewer_links.check_url`` and ``viewer_links.from_rows``), so a link in
the navigation can only open a web page. The icons are built in because the
content policy of the viewer allows an image from ``'self'`` only, so a link
cannot bring its own favicon.
"""

from __future__ import annotations

from typing import Any

from joshua_shared.config import Person, is_member
from joshua_shared.layout import wiki_root as wiki_dir
from joshua_shared.log import get_logger
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway import viewer_links as links_mod
from joshua_gateway.files_mcp.server import _atomic_write, data_root
from joshua_gateway.viewer_api.edit import (
    NOT_JSON,
    _answer,
    _bad,
    _commit_write,
    _csrf_token,
    _csrf_valid,
    _field,
    _fields,
    _origin_ok,
    _refused,
)

logger = get_logger("viewer")

# The scope of the CSRF token of the write. The list is one file, so one
# scope covers every row of it.
LINKS_CSRF = "links"


def _rows(links: list[links_mod.Link]) -> list[dict[str, str]]:
    """The links as the page and the sidebar read them."""
    return [{"title": link.title, "url": link.url, "icon": link.icon} for link in links]


async def links(request: Request, person: Person) -> JSONResponse:
    """The saved links, the built-in icons, and the write of a member."""
    saved = links_mod.load(wiki_dir(data_root()))
    can_edit = is_member(person.role)
    return _answer(
        {
            "links": _rows(saved),
            "icons": list(links_mod.ICONS),
            "can_edit": can_edit,
            "csrf": _csrf_token(person.id, LINKS_CSRF) if can_edit else None,
        }
    )


async def save(request: Request, person: Person) -> JSONResponse:
    """Replace the whole list with the rows of the body.

    A guest, a stale token and a cross-site origin are 403. A row the person
    can fix is 400 with the message of the ``LinkError``, and the file does
    not change. The answer carries the saved list, as the file now holds it.
    """
    from joshua_gateway import viewer  # ``viewer`` imports this package

    if not is_member(person.role):
        return _refused(403)
    fields: dict[str, Any] | None = await _fields(request)
    if fields is None:
        return _bad(NOT_JSON)
    if not _csrf_valid(person.id, LINKS_CSRF, _field(fields, "csrf")) or not _origin_ok(request):
        return _refused(403)
    try:
        saved = links_mod.from_rows(fields.get("links"))
    except links_mod.LinkError as exc:
        return _bad(str(exc))
    abs_path = links_mod.links_path(wiki_dir(data_root()))
    _atomic_write(abs_path, links_mod.dump(saved).encode())
    _commit_write(viewer._roots_for(person)["wiki"], abs_path, "links")
    logger.info({"message": "viewer write", "root": "wiki", "what": "links", "links": len(saved)})
    return _answer({"ok": True, "links": _rows(saved)})
