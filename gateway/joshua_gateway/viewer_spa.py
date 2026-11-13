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
``index.html`` is never cached, because a new build writes a new one. The
service worker of the installed app, ``sw.js``, is never cached either: the
browser reads it again to find a new version, and a stale copy would keep an
old shell alive.

The mount needs no session cookie. The build holds no data, only the code of
the app, and the sign-in form is a page of that app: a browser with no cookie
must load it to sign in. The app asks ``/api/v1/session``, gets a 401, and goes
to ``/sign-in``. Every JSON route and both document routes keep the sign-in.
With no build in the image, ``mount`` returns None and the viewer serves its
JSON API only, so a bare checkout and the test suite still work.
"""

from __future__ import annotations

from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

# The folder of the build, inside the package, and its entry file.
STATIC_DIR = "static"
INDEX = "index.html"

# The service worker of the installed app, at the root so its scope is ``/``.
WORKER = "sw.js"

# The files a browser must read again on every visit, and the two spellings
# of the root, which answers with the entry file.
FRESH = frozenset({INDEX, WORKER})
ROOT = frozenset({"", "."})

# The first segment of a path the app never claims.
NO_FALLBACK = frozenset({"api"})

# The first segment of a path the app always claims, dot or no dot: the
# content of the viewer, which the app reads from the JSON routes, and the
# editor of one page of it.
CONTENT = frozenset({"wiki", "shared", "attachments", "edit", "documents"})

# The cache headers: a hashed asset forever, the entry file and the worker
# never.
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
        # The root answers with the entry file, and it is the address the
        # installed app starts at, so it must never come from a stale cache.
        # Starlette normalises the root to ``.``.
        name = path.strip("/")
        if name in ROOT:
            name = INDEX
        if name in FRESH:
            response.headers["Cache-Control"] = _NO_CACHE
        elif path.startswith("assets/"):
            response.headers["Cache-Control"] = _IMMUTABLE
        return response


def mount() -> Mount | None:
    """The mount of the built app, or None when the image holds no build."""
    base = static_dir()
    if not (base / INDEX).is_file():
        return None
    return Mount("/", app=SpaStaticFiles(directory=base, html=True))
