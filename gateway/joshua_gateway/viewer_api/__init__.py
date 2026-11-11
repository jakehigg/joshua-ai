"""The JSON routes of the viewer, which the React app reads.

Every route of this package answers ``application/json``, never caches, and
sits behind the sign-in of a page (``viewer._require_auth``), so a request
with no credentials gets the same 401 as a page when ``viewer.anonymous`` is
off. Each handler takes ``(request, person)``.

A write route takes a JSON body, and the body carries the CSRF token of its
scope. Every write also checks the ``Origin`` header (``viewer_api.edit``).

Every path starts with ``API_PREFIX``. The prefix is in one constant and
``api_path`` builds every path from it, so no route can drift from it.

``viewer.build_app`` splices ``routes()`` into its route list, before the
mount of the React app and after the probes.
"""

from __future__ import annotations

from starlette.routing import Route

from joshua_gateway.viewer_api import (
    documents,
    edit,
    files,
    journal,
    links,
    me,
    search,
    session,
    usage,
    wiki,
)

# Every JSON route of the viewer is under this prefix, from the first one.
API_PREFIX = "/api/v1"


def api_path(suffix: str) -> str:
    """The full path of one JSON route: ``/api/v1/<suffix>``."""
    return f"{API_PREFIX}/{suffix.strip('/')}"


def routes() -> list[Route]:
    """The JSON routes, each behind the same sign-in as a page.

    The import of ``viewer`` is here, because ``viewer`` imports this package
    to build its app.
    """
    from joshua_gateway.viewer import _require_auth

    return [
        Route(api_path("session"), _require_auth(session.session)),
        Route(api_path("me"), _require_auth(me.me)),
        Route(api_path("journal"), _require_auth(journal.feed)),
        Route(api_path("journal/{year:int}/{month:int}/{day:int}"), _require_auth(journal.day)),
        Route(api_path("search"), _require_auth(search.search)),
        Route(api_path("usage"), _require_auth(usage.usage)),
        Route(api_path("links"), _require_auth(links.links)),
        Route(api_path("links"), _require_auth(links.save), methods=["POST"]),
        Route(api_path("documents"), _require_auth(documents.documents)),
        Route(api_path("documents/{owner}/{slug}"), _require_auth(documents.document)),
        Route(
            api_path("documents/{owner}/{slug}/comments"),
            _require_auth(documents.add_comment),
            methods=["POST"],
        ),
        Route(
            api_path("documents/{owner}/{slug}/comments/{cid}/delete"),
            _require_auth(documents.delete_comment),
            methods=["POST"],
        ),
        Route(api_path("wiki/tree"), _require_auth(wiki.tree)),
        Route(api_path("wiki"), _require_auth(wiki.content)),
        Route(api_path("files/{root}/{path:path}"), _require_auth(files.file)),
        Route(api_path("edit"), _require_auth(edit.edit_page)),
        Route(api_path("new"), _require_auth(edit.new_page)),
        Route(api_path("save"), _require_auth(edit.save), methods=["POST"]),
        Route(api_path("delete"), _require_auth(edit.delete), methods=["POST"]),
    ]
