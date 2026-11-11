"""The JSON routes of the viewer, which the React app reads.

Every route of this package answers ``application/json``, never caches, and
sits behind the sign-in of a page (``viewer._require_auth``), so a request
with no credentials gets the same 401 as a page when ``viewer.anonymous`` is
off. Each handler takes ``(request, person)``.

Every path starts with ``API_PREFIX``. The prefix is in one constant and
``api_path`` builds every path from it, so no route can drift from it.

``viewer.build_app`` splices ``routes()`` into its route list, before the
mount of the React app and after the probes.
"""

from __future__ import annotations

from starlette.routing import Route

from joshua_gateway.viewer_api import me, session, wiki

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
        Route(api_path("wiki/tree"), _require_auth(wiki.tree)),
    ]
