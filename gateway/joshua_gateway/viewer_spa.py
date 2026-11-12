"""Serving the React app of the viewer from the same container.

The image builds ``gateway/frontend`` and copies the result into
``joshua_gateway/static/``. This module mounts that folder at ``/`` as the
last route of the viewer, so every explicit route still wins and only the
rest reaches the app.

An address of the app is a path of its router, not a file, so an unknown path
gets ``index.html`` and the router reads the address. Three kinds of path
keep their 404 instead, because the app never claims them: a path whose last
segment holds a dot names a file, so a missing asset never arrives as HTML; and
a path under ``api/`` is the JSON space of the viewer, which covers
``api/v1/…``. The chat is a screen of the app, at ``/chat`` and
``/chat/<person>``, so it falls back like any other page.

The content sections of the viewer and the editor (``CONTENT``) are the
exception to the dot: a wiki page ends in ``.md``, an attachment keeps the
name it arrived with, the slug of a document may hold a dot, and the address
of the editor carries the path of the page it edits, so a path under one of
them always reaches the app. The two routes of a document that the server
answers itself (``<slug>.json`` and ``<slug>/artifact``) come before this
mount, so they win.

A built asset carries a content hash in its name, so it is immutable;
``index.html`` is never cached, because a new build writes a new one.

The mount is behind the sign-in of a page: a request with no credentials and
no ``viewer.anonymous`` gets the same 401 as a page. With no build in the
image, ``mount`` returns None and the viewer serves its server-rendered pages
only, so a bare checkout and the test suite still work.
"""

from __future__ import annotations

from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

# The folder of the build, inside the package, and its entry file.
STATIC_DIR = "static"
INDEX = "index.html"

# The first segment of a path the app never claims.
NO_FALLBACK = frozenset({"api"})

# The first segment of a path the app always claims, dot or no dot: the
# content of the viewer, which the app reads from the JSON routes, and the
# editor of one page of it.
CONTENT = frozenset({"wiki", "shared", "attachments", "edit", "documents"})

# The cache headers: a hashed asset forever, the entry file never.
_IMMUTABLE = "public, max-age=31536000, immutable"
_NO_CACHE = "no-cache"


def static_dir() -> Path:
    """The folder that holds the built app."""
    return Path(__file__).resolve().parent / STATIC_DIR


def falls_back(path: str) -> bool:
    """True when an unknown path gets ``index.html``."""
    clean = path.strip("/")
    first = clean.split("/", 1)[0]
    if first in NO_FALLBACK:
        return False
    if first in CONTENT:
        return True
    return "." not in clean.rsplit("/", 1)[-1]


class SpaStaticFiles(StaticFiles):
    """The built app: its files, and ``index.html`` for a path of its router."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or not falls_back(path):
                raise
            response = await super().get_response(INDEX, scope)
            response.headers["Cache-Control"] = _NO_CACHE
            return response
        if path.strip("/") == INDEX:
            response.headers["Cache-Control"] = _NO_CACHE
        elif path.startswith("assets/"):
            response.headers["Cache-Control"] = _IMMUTABLE
        return response


class SignedIn:
    """An ASGI app behind the sign-in of a page."""

    def __init__(self, app: StaticFiles) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from joshua_gateway import viewer  # ``viewer`` imports this module

        if viewer._authenticate(Request(scope, receive)) is None:
            await viewer._unauthorized()(scope, receive, send)
            return
        await self.app(scope, receive, send)


def mount() -> Mount | None:
    """The mount of the built app, or None when the image holds no build."""
    base = static_dir()
    if not (base / INDEX).is_file():
        return None
    return Mount("/", app=SignedIn(SpaStaticFiles(directory=base, html=True)))
