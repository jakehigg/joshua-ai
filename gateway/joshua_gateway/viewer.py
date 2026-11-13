"""The viewer: one React app in the gateway image, and the JSON API it reads.

A person opens a browser, signs in on a form, and gets the React app
(``gateway/frontend``), which the node stage of ``gateway/Dockerfile`` builds
and ``viewer_spa`` mounts at ``/``, behind the same sign-in as every other
route. Every screen is an address of its router: the one wiki (which carries
Joshua's own journal and its profile of each person) at ``/`` and
``/wiki/…``, a person's own attachments at ``/attachments/…`` and the shared
ones at ``/shared/…``, their own page at ``/me``, the journal as a feed at
``/journal`` and a day at ``/journal/<year>/<month>/<day>``, the search at
``/search``, the usage at ``/usage``, the links at ``/links``, the documents
at ``/documents`` and ``/documents/<owner>/<slug>``, the assignments at
``/assignments``, ``/assignments/<id>``, ``/assignments/saved`` and
``/assignments/saved/<id>``, the editor at ``/edit/…`` and ``/new``, and the
chat at ``/chat/<person id>`` with a workspace at ``/chat/w/<slug>``. The
agent never calls the viewer.

The app reads and writes one JSON API under ``/api/v1/``, and
``viewer_api.routes`` is the one list of its routes: the session, ``/me``,
the wiki, the file bytes, the journal, the search, the usage, the links, the
documents with their comments, the assignments and the saved ones with
theirs, the edit, the new page, the save and the delete, the chat
(``chat/<person>/turns``, which streams one turn, and
``chat/<person>/history``), and the nine workspace routes. The chat and the
workspaces are routes of the viewer, not another app: the chat reaches the
channels container through ``viewer_channels`` and ``viewer_multipart``, and
the workspaces proxy core's ``/v1/workspaces`` through
``viewer_api.workspaces`` and ``workspace_items``. The viewer also calls core
for the search by meaning (``viewer_search``) and the tokens of each group by
day (``viewer_usage``), both when ``CORE_URL`` and ``JOSHUA_TOKEN_VIEWER``
are set, and for the subscription limits (``viewer_limits``), which need
``CORE_URL`` only, because core's ``GET /v1/limits`` has no bearer.
``/assignments`` shows the assignments of the person who signed in, read from
the assignments manager when ``ASSIGNMENTS_URL`` and ``JOSHUA_TOKEN_VIEWER``
are set (``viewer_assignments``). ``/documents`` shows the documents of the
member who signed in and the shared ones, read from ``/data/documents``
(``viewer_api.documents``); a guest has no such section.

Two routes of a document are the only ones outside ``/api/v1/``, because the
chat calls both: ``<slug>.json`` for the head of an artifact card, and
``<slug>/artifact`` for the frame of an HTML document, which gets its own
content policy (``viewer_documents.artifact_policy``). The viewer reads the
data volume through the same resolver as the files MCP (``files_mcp.paths``),
so the two never drift on who reads what.

A member has three write actions, all human actions in a browser that the
agent never gets: an edit of a wiki page or a journal entry, a new wiki page,
and a delete, which moves a wiki page to the trash under ``wiki/.trash/`` and
is never a hard delete. The editor is the React app (``/edit/…`` and
``/new``), and it writes through the JSON routes of ``viewer_api.edit``, which
hold the checks of every write. A guest reads. A person can also comment on a line of the
definition, the plan, or the report of their own assignment, and answer a
decision of its report: the viewer sends the comment to the assignments
manager as that person, and writes nothing itself. A member can
comment on a line of a document of their own or a shared one through
``viewer_api.documents``: the viewer writes the comment to the comments file
of the document and commits it.

A sign-in is one signed cookie, ``joshua_session``: the person proves who
they are on the form of the sign-in page, core checks the secret, and the
cookie carries the person from then on (``viewer_session``, and the routes of
``viewer_api.session``). The viewer holds no password of its own, and there is
no HTTP basic path.

``viewer.anonymous`` opens the viewer: a request with no cookie gets full
access, read and write, as a synthetic "anonymous" identity, so a private
network needs no sign-in. When ``viewer.anonymous`` names a member, such a
request is served as that member, as if they signed in. A request that does
carry a cookie is still checked, so a member can sign in on an open viewer
too.

Start it with ``python -m joshua_gateway.viewer``. It refuses to start when
``viewer.enabled`` is false. Put a reverse proxy in front for TLS; the viewer
serves plain HTTP and binds every route behind the cookie except the probes
and the four routes of the sign-in itself.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from joshua_shared import config, log
from joshua_shared.config import Person, is_member
from joshua_shared.layout import is_hidden
from joshua_shared.log import get_logger, install_healthcheck_filter
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Route

from joshua_gateway import viewer_api, viewer_channels, viewer_session, viewer_spa
from joshua_gateway import viewer_assignments as assignments_mod
from joshua_gateway import viewer_docstore as docstore
from joshua_gateway import viewer_documents as documents_mod
from joshua_gateway import viewer_limits as limits_mod
from joshua_gateway import viewer_search as search_mod
from joshua_gateway.files_mcp.paths import Root
from joshua_gateway.files_mcp.server import data_root
from joshua_gateway.viewer_api.edit import (
    _csrf_token,  # noqa: F401 — the token of a scope, which the tests of a write mint
)

logger = get_logger("viewer")

# The person's home page shows at most this many recent attachments.
HOME_ATTACHMENTS_MAX = 30

# The metadata file a channels-stored attachment carries: ``<file>.meta.json``. Never
# a piece of content on its own, so a listing hides it.
_META_SUFFIX = ".meta.json"

# The identity a request with no cookie gets when ``viewer.anonymous`` is
# true. It is not in ``people``: it has full member access, but the home page
# shows it no profile link and no personal files folder, because there is no
# such person on disk.
ANONYMOUS_PERSON_ID = "anonymous"
_ANONYMOUS_PERSON = Person(id=ANONYMOUS_PERSON_ID, name="Anonymous", role="member")

# A JSON answer a client polls is never cached.
_NO_STORE = {"Cache-Control": "no-store"}


# -- auth -------------------------------------------------------------------


def _anonymous_person(cfg: config.JoshuaConfig) -> Person | None:
    """The person a request with no cookie gets, or None.

    ``viewer.anonymous: true`` gives the synthetic anonymous identity, which
    has full member access. A person id gives that member, exactly as if they
    signed in. The config check makes sure that the id names a member.
    """
    anonymous = cfg.viewer.anonymous
    if anonymous is True:
        return _ANONYMOUS_PERSON
    if isinstance(anonymous, str):
        return cfg.person(anonymous)
    return None


async def _authenticate(request: Request) -> Person | None:
    """Return the signed-in person, or None for a cookie that does not stand.

    A request with no ``joshua_session`` cookie is served as the person of
    ``_anonymous_person``, so a private network needs no sign-in. A request
    that does carry a cookie is always checked (``viewer_session``): one this
    viewer did not sign, an expired one, and one whose stamp is not the one
    core holds for that person's secret all fail the same way, and so does a
    person who has left the roster. None of them is quietly served as the
    anonymous identity.
    """
    cfg = config.load()
    if not request.cookies.get(viewer_session.COOKIE_NAME):
        return _anonymous_person(cfg)
    person_id = await viewer_session.person_from_cookie(request)
    if person_id is None:
        return None
    return cfg.person(person_id)


def _unauthorized() -> Response:
    """The 401 of every route: plain JSON, and no challenge.

    The sign-in is a form of the React app, so the browser must never put up
    its own dialog: there is no ``WWW-Authenticate`` header here.
    """
    return JSONResponse({"error": "sign in to read this"}, status_code=401, headers=_NO_STORE)


def _require_auth(handler: Any) -> Any:
    """Wrap a handler so it runs only for a signed-in person, else returns 401."""

    async def wrapper(request: Request) -> Response:
        person = await _authenticate(request)
        if person is None:
            return _unauthorized()
        return await handler(request, person)

    return wrapper


def _roots_for(person: Person) -> dict[str, Root]:
    """The display roots of the viewer, which are still per person.

    The corpus is shared, and the files MCP keys no root on a person. The viewer
    does, because its URL space says "mine": ``/attachments/`` names no person,
    so it can only mean the person who signed in. A view of the whole corpus
    needs a URL for each person, which is a separate change.

    This is a view, not a boundary. The viewer is read-only for everybody except
    a member deleting a wiki page, and it authenticates its own reader. Nothing
    here grants what the files MCP would refuse.
    """
    root = data_root()
    home = root / "people" / person.id
    member = is_member(person.role)
    return {
        "wiki": Root("wiki", root / "wiki", can_write=member, md_only=True),
        "attachments": Root("attachments", home / "attachments", can_write=False, md_only=False),
        "shared": Root("shared", root / "shared", can_write=False, md_only=False),
    }


# -- reading ----------------------------------------------------------------


def _split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Split leading ``---`` YAML frontmatter from the markdown body.

    A ``---`` opener with no closing fence is a horizontal rule, not
    frontmatter, so it returns ``(None, text)``.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            try:
                data = yaml.safe_load("\n".join(lines[1:index]))
            except yaml.YAMLError:
                return None, text
            body = "\n".join(lines[index + 1 :])
            return (data if isinstance(data, dict) else None), body
    return None, text


def _read_text(abs_path: Path) -> str | None:
    try:
        return abs_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


# -- listings ---------------------------------------------------------------


def _iter_markdown(root: Root) -> Iterator[tuple[Path, str]]:
    """Yield ``(abs_path, root-relative posix path)`` for each markdown file in a
    root, skipping a dot entry such as ``.trash`` or ``.git`` at any depth."""
    if not root.base.is_dir():
        return
    for item in sorted(root.base.rglob("*.md")):
        if not item.is_file() or is_hidden(item, root.base):
            continue
        yield item, item.relative_to(root.base).as_posix()


def _list_attachments(root: Root) -> list[str]:
    """Every stored attachment under a person's own ``attachments/``, newest
    first, skipping a dot entry and the ``.meta.json`` metadata file next to a file."""
    if not root.base.is_dir():
        return []
    names = [
        item.relative_to(root.base).as_posix()
        for item in root.base.rglob("*")
        if item.is_file()
        and not is_hidden(item, root.base)
        and not item.name.endswith(_META_SUFFIX)
    ]
    names.sort(reverse=True)
    return names


# -- routes -----------------------------------------------------------------


def _clock() -> tuple[tzinfo, datetime]:
    tz = limits_mod.zone(config.load().timezone)
    return tz, _now(tz)


# -- documents --------------------------------------------------------------
#
# A member reads their own documents and the shared ones; a guest gets none.
# The list and the page are the React app, which reads the JSON routes of
# ``viewer_api.documents``. Two routes stay here, because the chat calls
# both: the frontmatter of a document, and the body of an HTML document for
# the frame of its page.


def _artifact_not_found() -> Response:
    """The 404 of the artifact route: plain text, never a page.

    The frame that loads this address shows the document and nothing else,
    and every page of the viewer is the React app, so the module renders no
    HTML of its own.
    """
    return Response("not found", status_code=404, media_type="text/plain")


def _document(request: Request, person: Person) -> docstore.Document | None:
    """The document of the URL, when the person may read it."""
    if not is_member(person.role):
        return None
    base = docstore.root()
    if not base.is_dir():
        return None
    owner = request.path_params["owner"]
    slug = request.path_params["slug"]
    return docstore.find(base, owner, slug, docstore.owners(person.id))


async def document_meta(request: Request, person: Person) -> Response:
    """The title, the kind, the status, and the ``updated`` time of one
    document, as JSON. A document of another member is 404, as is one that is
    not there.

    The chat reads the title of an artifact card here, and polls ``updated``
    to see a change that no turn made. The answer holds no part of the body,
    renders nothing, and reads no git history, so a poll stays cheap. A slug
    that ends in ``.json`` is not a document, so this URL always names the
    frontmatter of ``<slug>``."""
    doc = _document(request, person)
    if doc is None:
        return JSONResponse({"error": "not found"}, status_code=404, headers=_NO_STORE)
    return JSONResponse(
        {"title": doc.title, "kind": doc.kind, "status": doc.status, "updated": doc.updated},
        headers=_NO_STORE,
    )


async def document_artifact(request: Request, person: Person) -> Response:
    """The body of an HTML document alone, for the frame of its page.
    ``?rev=<sha>`` gives an older version.

    The frame of the document page loads this address, so the document gets
    its own content policy (``documents_mod.artifact_policy``): its styles
    render, its scripts run only with ``scripts: true`` in its frontmatter,
    and it reaches no network. The frame keeps its ``sandbox``, so the
    document also has an opaque origin and no access to the viewer. The
    sign-in and the owner rule are those of the page, so a document of
    another member and one that is not there are both 404, as is a Markdown
    document, which has no frame."""
    doc = _document(request, person)
    if doc is None or doc.format != "html":
        return _artifact_not_found()
    rev = request.query_params.get("rev")
    if rev:
        found = docstore.at_version(docstore.root(), doc, rev)
        if found is None:
            return _artifact_not_found()
        doc = found[1]
    return HTMLResponse(
        doc.body,
        headers={
            **_NO_STORE,
            "Content-Security-Policy": documents_mod.artifact_policy(doc.scripts),
        },
    )


def _now(tz: tzinfo) -> datetime:
    """The time now in ``tz``. A test replaces it to fix the clock."""
    return datetime.now(tz)


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def readyz(request: Request) -> JSONResponse:
    """Readiness with booleans and counts only, no person name and no secret."""
    cfg = config.load()
    return JSONResponse(
        {
            "ok": True,
            "enabled": cfg.viewer.enabled,
            "users": len(cfg.viewer.users),
            "anonymous": cfg.viewer.anonymous is not False,
            "data": data_root().is_dir(),
            "core_search": search_mod.from_env(os.environ) is not None,
            "assignments": assignments_mod.from_env(os.environ) is not None,
            "chat": bool(
                os.environ.get(viewer_channels.CHANNELS_URL_ENV)
                and os.environ.get(viewer_channels.VIEWER_TOKEN_ENV)
            ),
        }
    )


class SecurityHeaders(BaseHTTPMiddleware):
    """Add the fixed security headers to every response.

    ``default-src 'none'`` blocks every sub-resource; the page loads its
    stylesheets, its images, its scripts and its data from ``'self'``. The
    viewer serves a JavaScript bundle of its own, so ``script-src`` holds
    ``'self'`` alone. No inline script runs, and no hash is allowed.

    The viewer is an installable app: ``manifest-src 'self'`` allows its
    manifest and ``worker-src 'self'`` allows its service worker, both files
    of this origin and of no other.

    ``frame-ancestors 'self'`` lets a page of the viewer, the chat and its
    artifact panel, frame a document page of the same origin. No other
    origin can frame the viewer: the value is always ``'self'``, never a
    host and never ``*``.

    The artifact route of an HTML document sets its own policy, which is
    stricter on the network and allows the styles of the document. A
    response that carries a policy keeps it; every other response gets this
    one.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self'; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; "
            "manifest-src 'self'; worker-src 'self'; "
            "frame-src 'self'; frame-ancestors 'self'",
        )
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def build_app() -> Starlette:
    """Build the viewer app. Auth binds every route except the probes.

    The JSON routes of ``viewer_api`` come after the probes, and the mount of
    the React app is the last route, so every explicit route wins over it.
    Every page, the stylesheets and the images are files of the app, so they
    are not here: with no build in the image, the viewer serves the JSON API
    and nothing else.
    """
    routes: list[BaseRoute] = [
        Route("/healthz", healthz),
        Route("/readyz", readyz),
        *viewer_api.routes(),
        # The two routes of a document that the chat calls. The page itself
        # is the React app, and a slug never ends in ``.json``, so these two
        # and an address of the app can never name the same file.
        Route("/documents/{owner}/{slug}.json", _require_auth(document_meta)),
        Route("/documents/{owner}/{slug}/artifact", _require_auth(document_artifact)),
    ]
    spa = viewer_spa.mount()
    if spa is None:
        logger.info({"message": "no frontend build; the React pages are off"})
    else:
        routes.append(spa)
    return Starlette(middleware=[Middleware(SecurityHeaders)], routes=routes)


app = build_app()


def main() -> None:
    log.configure_from_env("joshua-viewer")
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not cfg.viewer.enabled:
        logger.error(
            {"message": "viewer is disabled; set viewer.enabled: true in joshua.yaml to start it"}
        )
        raise SystemExit(1)
    install_healthcheck_filter()
    logger.info(
        {
            "message": "viewer up",
            "users": len(cfg.viewer.users),
            "anonymous": cfg.viewer.anonymous is not False,
        }
    )
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)


if __name__ == "__main__":
    main()
