"""Web viewer for the wiki, the journal, and a person's own attachments.

A person opens a browser, signs in with HTTP basic, and reads the one wiki
(which carries Joshua's own journal and its profile of each person), their own
attachments, and the shared attachments. ``/journal`` shows the journal as a
feed, newest day first, because the wiki tree is the wrong shape for reading
what happened. The agent never calls the viewer. The viewer calls core for two
things, when ``CORE_URL`` and ``JOSHUA_TOKEN_VIEWER`` are set: the search asks
core to search by meaning (``viewer_search``), and the usage page shows the
tokens of each group by day (``viewer_usage``) under the subscription limits
(``viewer_limits``). The limits need ``CORE_URL`` only, because core's
``GET /v1/limits`` has no bearer. ``/assignments`` shows the assignments of
the person who signed in, read from the assignments manager when
``ASSIGNMENTS_URL`` and ``JOSHUA_TOKEN_VIEWER`` are set
(``viewer_assignments``). ``/documents`` shows the documents of the member
who signed in and the shared ones, read from ``/data/documents``
(``viewer_documents``); a guest has no such section. The navigation also holds
a Chat item at ``/chat/<person id>`` for the person who signed in. The chat is
another app on the same host, so the viewer only links to it. The viewer reads
the data volume through the same resolver as the files MCP
(``files_mcp.paths``), so the two never drift on who reads what.

The wiki, the shared attachments, a person's own attachments, the journal, the
search, the usage, the links and the editor are the React app: ``/``,
``/wiki/…``, ``/shared/…``, ``/attachments/…``, ``/journal``,
``/journal/<year>/<month>/<day>``, ``/search``, ``/usage``, ``/links``,
``/edit/…`` and ``/new`` are addresses of its router (``viewer_spa``), and it
reads ``GET /api/v1/wiki`` for a page, a folder or a file,
``GET /api/v1/files/<root>/<path>`` for the bytes,
``GET /api/v1/journal`` for a month of the feed,
``GET /api/v1/journal/<year>/<month>/<day>`` for a day,
``GET /api/v1/search`` for both halves of the search,
``GET /api/v1/usage`` for the limits and the tokens by day,
``GET /api/v1/links`` for the links of the sidebar and of the page,
``POST /api/v1/links`` for the list a member saves, and
``GET /api/v1/edit``, ``GET /api/v1/new``, ``POST /api/v1/save`` and
``POST /api/v1/delete`` for a write (``viewer_api``).

A member has three write actions, all human actions in a browser that the
agent never gets: an edit of a wiki page or a journal entry, a new wiki page,
and a delete, which moves a wiki page to the trash under ``wiki/.trash/`` and
is never a hard delete. The editor is the React app (``/edit/…`` and
``/new``), and it writes through the JSON routes of ``viewer_api.edit``, which
hold the checks of every write. A guest reads. A person can also comment on a line of the
definition, the plan, or the report of their own assignment, and answer a
decision of its report: the viewer sends the comment to the assignments
manager as that person, and writes nothing itself. A member can
comment on a line of a document of their own or a shared one: the viewer
writes the comment to the comments file of the document and commits it.

``viewer.anonymous`` opens the viewer: a request with no ``Authorization``
header gets full access, read and write, as a synthetic "anonymous" identity,
so a private network needs no sign-in. When ``viewer.anonymous`` names a
member, such a request is served as that member, as if they signed in. A
request that carries credentials is still checked, so a member can sign in by
password too. ``viewer.users`` is how a household adds restrictions back.

Start it with ``python -m joshua_gateway.viewer``. It refuses to start when
``viewer.enabled`` is false. Put a reverse proxy in front for TLS; the viewer
serves plain HTTP and binds every route behind basic auth except the probes.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import os
import sys
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime, tzinfo
from functools import cache
from html import escape
from importlib import resources
from pathlib import Path
from typing import Any

import bcrypt
import uvicorn
import yaml
from joshua_shared import config, log
from joshua_shared.config import Person
from joshua_shared.layout import is_hidden
from joshua_shared.layout import wiki_root as wiki_dir
from joshua_shared.log import get_logger, install_healthcheck_filter
from markdown_it import MarkdownIt
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from joshua_gateway import viewer_api, viewer_spa
from joshua_gateway import viewer_assignments as assignments_mod
from joshua_gateway import viewer_comments as comments_mod
from joshua_gateway import viewer_docstore as docstore
from joshua_gateway import viewer_documents as documents_mod
from joshua_gateway import viewer_limits as limits_mod
from joshua_gateway import viewer_links as links_mod
from joshua_gateway import viewer_search as search_mod
from joshua_gateway import viewer_showcase as showcase_mod
from joshua_gateway.files_mcp.paths import Root
from joshua_gateway.files_mcp.server import data_root
from joshua_gateway.viewer_api.edit import _csrf_token, _csrf_valid, _origin_ok

logger = get_logger("viewer")

# The basic-auth realm.
REALM = "joshua viewer"

# Where a viewer password can come from, besides the entry in ``viewer.users``.
# ``VIEWER_PASSWORDS`` carries every one of them in a single variable, so a
# tracked file can pass them all through and name no person.
VIEWER_PASSWORDS_ENV = "VIEWER_PASSWORDS"
VIEWER_PW_PREFIX = "VIEWER_PW_"

# The person's home page shows at most this many recent attachments.
HOME_ATTACHMENTS_MAX = 30

# The metadata file a channels-stored attachment carries: ``<file>.meta.json``. Never
# a piece of content on its own, so a listing hides it.
_META_SUFFIX = ".meta.json"

# The identity a request with no credentials gets when ``viewer.anonymous`` is
# true. It is not in ``people``: it has full member access, but the home page
# shows it no profile link and no personal files folder, because there is no
# such person on disk.
ANONYMOUS_PERSON_ID = "anonymous"
_ANONYMOUS_PERSON = Person(id=ANONYMOUS_PERSON_ID, name="Anonymous", role="member")

# Raw HTML is disabled, so a ``<script>`` in a markdown file renders as text.
# The commonmark preset has no tables; the table and strikethrough rules are
# turned on, with no new dependency.
_MD = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable(["table", "strikethrough"])

# A JSON answer a client polls is never cached.
_NO_STORE = {"Cache-Control": "no-store"}

# The stylesheets and images the pages load. The shared Joshua stylesheet and
# its images are in ``ui/``, and ``viewer.css`` holds what only the viewer
# needs. Each is served from the package, behind the same sign-in as a page.
_UI_DIR = "ui"
_UI_FILES = {
    "joshua-ui.css": "text/css",
    "logo-512.png": "image/png",
    "logo-64.png": "image/png",
    "apple-touch-icon.png": "image/png",
    "favicon-32.png": "image/png",
}
_VIEWER_CSS = "viewer.css"


# -- auth -------------------------------------------------------------------


def _basic_credentials(request: Request) -> tuple[str, str] | None:
    """Return the ``(user, password)`` from a basic ``Authorization`` header, or
    None when the header is absent or malformed."""
    header = request.headers.get("Authorization", "")
    scheme, _, param = header.partition(" ")
    if scheme.lower() != "basic" or not param:
        return None
    try:
        decoded = base64.b64decode(param, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    user, sep, password = decoded.partition(":")
    if not sep:
        return None
    return user, password


def _check_password(reference: str, provided: str) -> bool:
    """Check ``provided`` against a password reference.

    A reference that starts with ``$2`` is a bcrypt hash and is checked with
    bcrypt. Any other value is a literal password compared with
    ``hmac.compare_digest``. An empty reference never matches.
    """
    if not reference:
        return False
    if reference.startswith("$2"):
        try:
            return bcrypt.checkpw(provided.encode("utf-8"), reference.encode("utf-8"))
        except ValueError:
            return False
    return hmac.compare_digest(reference, provided)


def _passwords_from_env() -> dict[str, str]:
    """Parse ``VIEWER_PASSWORDS`` into person id -> password reference.

    One variable carries every password, so the compose file names no person.
    The format is ``<id>=<reference>``, comma separated. A reference is a
    bcrypt hash, which holds no comma; a literal password with a comma in it
    cannot be carried here, and a hash is the wanted form anyway.
    """
    out: dict[str, str] = {}
    for item in os.environ.get(VIEWER_PASSWORDS_ENV, "").split(","):
        person_id, separator, reference = item.strip().partition("=")
        if separator and person_id.strip() and reference.strip():
            out[person_id.strip()] = reference.strip()
    return out


def _password_reference(cfg: Any, user: str) -> str:
    """The password reference for ``user``, or "" when there is none.

    ``viewer.users`` says who may sign in, and the key must be there. The
    reference itself comes from the entry, from ``VIEWER_PASSWORDS``, or from
    ``VIEWER_PW_<ID>``, in that order. An id that is not a key of
    ``viewer.users`` gets nothing, whatever the environment holds.
    """
    if user not in cfg.viewer.users:
        return ""
    reference = cfg.viewer.users.get(user) or ""
    if reference:
        return reference
    reference = _passwords_from_env().get(user, "")
    if reference:
        return reference
    return os.environ.get(f"{VIEWER_PW_PREFIX}{user.upper().replace('-', '_')}", "")


def _anonymous_person(cfg: config.JoshuaConfig) -> Person | None:
    """The person a request with no credentials gets, or None.

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


def _authenticate(request: Request) -> Person | None:
    """Return the signed-in person, or None for a missing or wrong credential.

    A wrong user and a wrong password fail the same way. A person named in
    ``viewer.users`` who is no longer in ``people`` cannot sign in.

    A request with no ``Authorization`` header is served as the person of
    ``_anonymous_person``, so a private network needs no password. A request
    that does carry credentials is always checked against ``viewer.users``, so
    a member can still sign in by password too.
    """
    cfg = config.load()
    creds = _basic_credentials(request)
    if creds is None:
        return _anonymous_person(cfg)
    user, password = creds
    if not _check_password(_password_reference(cfg, user), password):
        return None
    return cfg.person(user)


def _unauthorized() -> Response:
    return Response(
        "authentication required",
        status_code=401,
        media_type="text/plain",
        headers={"WWW-Authenticate": f'Basic realm="{REALM}"'},
    )


# The person of the request that is served now, for the navigation.
_REQUEST_PERSON: ContextVar[Person | None] = ContextVar("viewer_person", default=None)


def _require_auth(handler: Any) -> Any:
    """Wrap a handler so it runs only for a signed-in person, else returns 401."""

    async def wrapper(request: Request) -> Response:
        person = _authenticate(request)
        if person is None:
            return _unauthorized()
        token = _REQUEST_PERSON.set(person)
        try:
            return await handler(request, person)
        finally:
            _REQUEST_PERSON.reset(token)

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
    member = person.role == "member"
    return {
        "wiki": Root("wiki", root / "wiki", can_write=member, md_only=True),
        "attachments": Root("attachments", home / "attachments", can_write=False, md_only=False),
        "shared": Root("shared", root / "shared", can_write=False, md_only=False),
    }


# -- rendering --------------------------------------------------------------


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


def _fmt_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _frontmatter_html(front: dict[str, Any] | None) -> str:
    if not front:
        return ""
    rows = "".join(
        f"<div><span class=k>{escape(str(key))}:</span> "
        f"<span class=v>{escape(_fmt_value(value))}</span></div>"
        for key, value in front.items()
    )
    return f'<header class="frontmatter">{rows}</header>'


# The sections of the navigation: (key, label, URL, icon path data).
_SECTIONS = (
    (
        "wiki",
        "Wiki",
        "/wiki/",
        '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5z"/>'
        '<path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/>',
    ),
    (
        "journal",
        "Journal",
        "/journal",
        '<path d="M5 4h14a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/>'
        '<path d="M8 2v4M16 2v4M3 10h18"/>',
    ),
    (
        "search",
        "Search",
        "/search",
        '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    ),
    ("usage", "Usage", "/usage", '<path d="M4 20V11M10 20V4M16 20v-8M21 20H3"/>'),
    (
        "assignments",
        "Assignments",
        "/assignments",
        '<path d="M9 4h6v3H9z"/>'
        '<path d="M9 5H6a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1h-3"/>'
        '<path d="m9 14 2 2 4-4"/>',
    ),
    (
        "documents",
        "Documents",
        "/documents",
        '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
        '<path d="M14 3v5h5M9 13h6M9 17h6"/>',
    ),
    ("chat", "Chat", "/chat", links_mod.ICONS["chat"]),
)
# The sections that a guest does not get.
_MEMBER_SECTIONS = frozenset({"documents"})
# The sections whose URL ends with the id of the person of the request. The
# chat answers at ``/chat/<person id>``, so the item follows the identity of
# the person who reads the viewer.
_PERSON_SECTIONS = frozenset({"chat"})
_SECTION_LABEL = {key: label for key, label, _, _ in _SECTIONS} | {"links": "Links", "me": "You"}
_SEARCH_ICON = _SECTIONS[2][3]
_ME_ICON = '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>'


def _icon(paths: str) -> str:
    return f'<svg class="j-icon" viewBox="0 0 24 24" aria-hidden="true">{paths}</svg>'


def _current(key: str, section: str) -> str:
    return ' aria-current="page"' if key == section else ""


def _sections() -> list[tuple[str, str, str, str]]:
    """The sections of the person of this request: a guest gets no Documents.

    A section of ``_PERSON_SECTIONS`` gets the id of the person on its URL, and
    a request with no person does not get it at all.
    """
    person = _REQUEST_PERSON.get()
    member = person is not None and person.role == "member"
    sections: list[tuple[str, str, str, str]] = []
    for key, label, url, icon in _SECTIONS:
        if not member and key in _MEMBER_SECTIONS:
            continue
        if key in _PERSON_SECTIONS:
            if person is None:
                continue
            url = f"{url}/{person.id}"
        sections.append((key, label, url, icon))
    return sections


def _shell(body: str, *, section: str) -> str:
    """The page shell: the sidebar on a wide screen; the app bar and the tabs
    on a phone. ``section`` marks the current item. The search field is in the
    app bar, so it shows on every screen; it loads ``/search``, which the
    React app renders."""
    sections = _sections()
    items = "".join(
        f'<a class="j-nav-item" href="{url}"{_current(key, section)}>{_icon(icon)}{label}</a>'
        for key, label, url, icon in sections
    )
    tabs = (
        "".join(
            f'<a class="j-tab" href="{url}"{_current(key, section)}>{label}</a>'
            for key, label, url, _ in sections
        )
        + f'<a class="j-tab" href="/links"{_current("links", section)}>Links</a>'
    )
    links_nav = links_mod.nav_html(
        links_mod.load(wiki_dir(data_root())), current=section == "links"
    )
    here = _SECTION_LABEL.get(section, "Home")
    return (
        '<div class="j-shell has-sidebar">'
        '<aside class="j-sidebar">'
        '<a class="j-brand" href="/"><img class="j-logo" src="/ui/logo-64.png" alt="">Joshua</a>'
        f'<nav class="v-sections" aria-label="Sections">{items}</nav>'
        f"{links_nav}"
        '<nav class="v-sections v-you" aria-label="You">'
        f'<a class="j-nav-item" href="/me"{_current("me", section)}>{_icon(_ME_ICON)}Your page</a>'
        "</nav>"
        "</aside>"
        '<div class="j-main">'
        '<header class="j-appbar">'
        '<img class="j-logo" src="/ui/logo-64.png" alt="">'
        f'<a class="j-appbar-title" href="/"><small>Joshua</small><strong>{here}</strong></a>'
        '<span class="j-spacer"></span>'
        '<form class="v-search" action="/search" method="get" role="search">'
        f'<label class="j-field">{_icon(_SEARCH_ICON)}'
        '<input type="search" name="q" placeholder="Search" aria-label="Search">'
        "</label></form>"
        "</header>"
        f'<nav class="j-tabs mobile-only" aria-label="Sections">{tabs}</nav>'
        f'<main class="j-content">{body}</main>'
        "</div></div>"
    )


def _page(
    title: str,
    body: str,
    *,
    status: int = 200,
    nav: bool = True,
    section: str = "",
    head: str = "",
) -> HTMLResponse:
    """A full page. ``section`` is the key of the current section in the
    navigation (``wiki``, ``journal``, ``search``, ``usage``,
    ``assignments``, ``documents``, ``chat``), or "" for the home page. ``nav`` False gives the bare
    content with no navigation. ``head`` is more HTML for the ``<head>``,
    such as a ``<meta>`` refresh."""
    main = _shell(body, section=section) if nav else f'<main class="j-content">{body}</main>'
    html = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        '<meta name="theme-color" content="#080d17">'
        f"<title>{escape(title)}</title>{head}"
        '<link rel=icon type="image/png" href="/ui/favicon-32.png">'
        '<link rel=apple-touch-icon href="/ui/apple-touch-icon.png">'
        '<link rel=stylesheet href="/ui/joshua-ui.css">'
        '<link rel=stylesheet href="/style.css"></head><body>'
        f"{main}</body></html>"
    )
    return HTMLResponse(html, status_code=status)


def _not_found() -> HTMLResponse:
    return _page("not found", '<h1 class="j-doc-title">not found</h1>', status=404, nav=False)


def _forbidden() -> HTMLResponse:
    return _page("forbidden", '<h1 class="j-doc-title">forbidden</h1>', status=403, nav=False)


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


def _list_markdown(root: Root) -> list[str]:
    return [rel for _, rel in _iter_markdown(root)]


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


async def assignments_list(request: Request, person: Person) -> Response:
    """The assignments of the person who signed in, one state at a time.
    With no ``state``, the page shows those in progress when there is one,
    else the drafts when there is one, else the completed ones. The page
    answers 200 also when the manager is not configured or does not answer,
    and says so; then no pill is selected."""
    client = assignments_mod.from_env(os.environ)
    tz, now = _clock()
    state = assignments_mod.parse_state(request.query_params.get("state"))
    body = None
    if client is None:
        state = None
    elif state is None:
        for state in ("in_progress", "draft", "completed"):
            body = await client.list(person.id, state)
            if body is None:
                state = None
                break
            if body.items:
                break
    else:
        body = await client.list(person.id, state)
        if body is None:
            state = None
    html = assignments_mod.list_html(
        body, state=state, configured=client is not None, tz=tz, now=now
    )
    return _page("assignments", html, section="assignments")


async def assignment_page(request: Request, person: Person) -> Response:
    """One assignment of the person who signed in, as a document. A row of
    another person is 404, as the manager answers."""
    client = assignments_mod.from_env(os.environ)
    title = f"assignment {request.path_params['id']}"
    if client is None:
        html = assignments_mod.unavailable_html(title, configured=False)
        return _page(title, html, section="assignments")
    item = await client.detail(person.id, request.path_params["id"])
    if item is assignments_mod.NOT_FOUND:
        return _not_found()
    if not isinstance(item, assignments_mod.Detail):
        html = assignments_mod.unavailable_html(title, configured=True)
        return _page(title, html, section="assignments")
    tz, now = _clock()
    forms = _comment_forms(person, "assignment", item.id)
    return _page(
        item.title,
        assignments_mod.detail_html(item, tz=tz, now=now, forms=forms),
        section="assignments",
        head=assignments_mod.refresh_meta(item),
    )


async def saved_list(request: Request, person: Person) -> Response:
    """The saved assignments of the person who signed in."""
    client = assignments_mod.from_env(os.environ)
    body = await client.saved(person.id) if client is not None else None
    tz, now = _clock()
    html = assignments_mod.saved_list_html(body, configured=client is not None, tz=tz, now=now)
    return _page("saved assignments", html, section="assignments")


async def saved_page(request: Request, person: Person) -> Response:
    """One saved assignment of the person who signed in, with its runs."""
    client = assignments_mod.from_env(os.environ)
    title = "saved assignment"
    if client is None:
        html = assignments_mod.unavailable_html(title, configured=False)
        return _page(title, html, section="assignments")
    saved = await client.saved_detail(person.id, request.path_params["id"])
    if saved is assignments_mod.NOT_FOUND:
        return _not_found()
    if not isinstance(saved, assignments_mod.SavedDetail):
        html = assignments_mod.unavailable_html(title, configured=True)
        return _page(title, html, section="assignments")
    tz, now = _clock()
    forms = _comment_forms(person, "saved", saved.id)
    return _page(
        saved.name,
        assignments_mod.saved_detail_html(saved, tz=tz, now=now, forms=forms),
        section="assignments",
    )


def _comment_base(kind: str, parent_id: int) -> str:
    """The viewer path of an assignment (``assignment``) or a saved one (``saved``)."""
    return f"/assignments/saved/{parent_id}" if kind == "saved" else f"/assignments/{parent_id}"


def _comment_forms(person: Person, kind: str, parent_id: int) -> comments_mod.CommentForms:
    base = _comment_base(kind, parent_id)
    return comments_mod.CommentForms(
        action=f"{base}/comments", csrf=_csrf_token(person.id, f"comments:{base}")
    )


# The texts of an assignment that take a comment, and the anchor that the
# page goes back to after a comment on each. A saved assignment has only a
# definition.
COMMENT_TARGETS = ("definition", "plan", "report")
_TARGET_PREFIX = {"definition": "L", "plan": "P", "report": "R"}
_TARGET_SECTION = {"definition": "definition", "plan": "plan", "report": "lead"}


async def _comment_write(request: Request, person: Person, kind: str) -> Response:
    """Send one comment write of the person to the manager, then go back to the line.

    The form carries the CSRF token of the assignment, and a cross-site
    ``Origin`` is refused. ``target`` names the text (the definition, the
    plan, or the report). The manager writes the comment as the person who
    signed in, on their own row only, and a row of another person is 404."""
    client = assignments_mod.from_env(os.environ)
    if client is None:
        return _not_found()
    parent_id = int(request.path_params["id"])
    base = _comment_base(kind, parent_id)
    form = await request.form()
    token = str(form.get("csrf", ""))
    if not _csrf_valid(person.id, f"comments:{base}", token) or not _origin_ok(request):
        return _forbidden()
    target = str(form.get("target", "") or "definition")
    if target not in COMMENT_TARGETS or (kind == "saved" and target != "definition"):
        target = ""
    comment_id = request.path_params.get("cid")
    if not target:
        written: assignments_mod.Written | None = assignments_mod.Written(400, "a bad target")
        anchor = ""
    elif comment_id is not None:
        written = await client.delete_comment(person.id, kind, parent_id, int(comment_id))
        anchor = _TARGET_SECTION[target]
    else:
        try:
            line_no = int(str(form.get("line_no", "")))
        except ValueError:
            line_no = 0
        written = await client.add_comment(
            person.id,
            kind,
            parent_id,
            line_no=line_no,
            line_text=str(form.get("line_text", "")),
            body=str(form.get("body", "")),
            target=target,
        )
        anchor = f"{_TARGET_PREFIX[target]}{line_no}"
    if written is not None and written.status == 404:
        return _not_found()
    if written is None or not written.ok:
        reason = written.error if written is not None else "the manager did not answer"
        body = assignments_mod.message_html(
            "comment not stored",
            f'<p class="j-error">{escape(reason or "The manager refused the comment.")}</p>'
            f'<p><a href="{escape(base)}">Back</a></p>',
        )
        return _page("comment not stored", body, status=400, section="assignments")
    logger.info(
        {
            "message": "viewer comment",
            "kind": kind,
            "target": target,
            "delete": comment_id is not None,
        }
    )
    return RedirectResponse(f"{base}#{anchor}", status_code=303)


async def comment_on_assignment(request: Request, person: Person) -> Response:
    return await _comment_write(request, person, "assignment")


async def comment_on_saved(request: Request, person: Person) -> Response:
    return await _comment_write(request, person, "saved")


# -- documents --------------------------------------------------------------
#
# A member reads their own documents and the shared ones; a guest gets none.
# The one write is a comment in the comments file of a document, with the
# same CSRF token and origin check as every other form.


async def documents_list(request: Request, person: Person) -> Response:
    """The documents of the person who signed in and the shared ones, one
    status at a time. With no documents folder, the page says so and
    creates nothing."""
    if person.role != "member":
        return _not_found()
    status = documents_mod.parse_status(request.query_params.get("status"))
    kind = documents_mod.parse_kind(request.query_params.get("kind"))
    base = docstore.root()
    docs = None
    counts: dict[str, int] = {}
    if base.is_dir():
        docs = docstore.list_documents(base, docstore.owners(person.id))
        counts = {d.tool_path: docstore.open_count(d) for d in docs if d.status == status}
    tz, now = _clock()
    html = documents_mod.list_html(
        docs, status=status, kind=kind, person_id=person.id, comments=counts, tz=tz, now=now
    )
    return _page("documents", html, section="documents")


def _document(request: Request, person: Person) -> docstore.Document | None:
    """The document of the URL, when the person may read it."""
    if person.role != "member":
        return None
    base = docstore.root()
    if not base.is_dir():
        return None
    owner = request.path_params["owner"]
    slug = request.path_params["slug"]
    return docstore.find(base, owner, slug, docstore.owners(person.id))


def _document_forms(person: Person, doc: docstore.Document) -> comments_mod.CommentForms:
    base = doc.url
    return comments_mod.CommentForms(
        action=f"{base}/comments", csrf=_csrf_token(person.id, f"comments:{base}")
    )


async def document_page(request: Request, person: Person) -> Response:
    """One document. ``?rev=<sha>`` shows an older version, read-only. A
    document of another member is 404, as is one that is not there."""
    doc = _document(request, person)
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
    tz, now = _clock()
    html = documents_mod.document_html(
        shown,
        person_id=person.id,
        comments=docstore.read_comments(doc) if version is None else [],
        forms=_document_forms(person, doc) if version is None else None,
        versions=versions,
        version=version,
        tz=tz,
        now=now,
    )
    return _page(shown.title, html, section="documents")


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
        return _not_found()
    rev = request.query_params.get("rev")
    if rev:
        found = docstore.at_version(docstore.root(), doc, rev)
        if found is None:
            return _not_found()
        doc = found[1]
    return HTMLResponse(
        doc.body,
        headers={
            **_NO_STORE,
            "Content-Security-Policy": documents_mod.artifact_policy(doc.scripts),
        },
    )


def _comment_refused(doc: docstore.Document, reason: str) -> Response:
    body = documents_mod.message_html(
        "comment not stored",
        f'<p class="j-error">{escape(reason)}</p><p><a href="{escape(doc.url)}">Back</a></p>',
    )
    return _page("comment not stored", body, status=400, section="documents")


async def document_comment(request: Request, person: Person) -> Response:
    """Add a comment of the person to a document, or delete their own open
    comment, then go back to the line. One commit for each write."""
    doc = _document(request, person)
    if doc is None:
        return _not_found()
    form = await request.form()
    token = str(form.get("csrf", ""))
    if not _csrf_valid(person.id, f"comments:{doc.url}", token) or not _origin_ok(request):
        return _forbidden()
    base = docstore.root()
    comment_id = request.path_params.get("cid")
    if comment_id is not None:
        if not docstore.delete_comment(base, doc, author=person.id, comment_id=comment_id):
            return _not_found()
        logger.info({"message": "viewer comment", "kind": "document", "delete": True})
        return RedirectResponse(doc.url, status_code=303)
    body = str(form.get("body", "")).strip()
    try:
        line_no = int(str(form.get("line_no", "")))
    except ValueError:
        line_no = -1
    anchor = doc.line(line_no)
    if anchor is None:
        return _comment_refused(doc, "The line is not in the document.")
    if not body:
        return _comment_refused(doc, "The comment is empty.")
    if len(body) > comments_mod.MAX_COMMENT:
        return _comment_refused(doc, f"The comment exceeds {comments_mod.MAX_COMMENT} characters.")
    line_text = str(form.get("line_text", "")) or anchor
    docstore.add_comment(
        base,
        doc,
        author=person.id,
        line_no=line_no,
        line_text=line_text,
        body=body,
        now=_now(UTC),
    )
    logger.info({"message": "viewer comment", "kind": "document", "delete": False})
    return RedirectResponse(f"{doc.url}#L{line_no}", status_code=303)


def _now(tz: tzinfo) -> datetime:
    """The time now in ``tz``. A test replaces it to fix the clock."""
    return datetime.now(tz)


@cache
def _package_file(*parts: str) -> bytes:
    """A file of this package, read once."""
    return resources.files("joshua_gateway").joinpath(*parts).read_bytes()


async def style(request: Request, person: Person) -> Response:
    """The viewer's own stylesheet, which loads after the shared one."""
    return Response(_package_file(_VIEWER_CSS), media_type="text/css")


async def ui_file(request: Request, person: Person) -> Response:
    """One file of the shared Joshua look: the stylesheet or an image. A name
    that is not in ``_UI_FILES`` is 404."""
    name = request.path_params["name"]
    media_type = _UI_FILES.get(name)
    if media_type is None:
        return _not_found()
    return Response(_package_file(_UI_DIR, name), media_type=media_type)


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
        }
    )


class SecurityHeaders(BaseHTTPMiddleware):
    """Add the fixed security headers to every response.

    ``default-src 'none'`` blocks every sub-resource; the page loads its
    stylesheets, its images, its scripts and its data from ``'self'``. The
    viewer serves a JavaScript bundle of its own, so ``script-src`` holds
    ``'self'``, and the hash of the copy script of a server-rendered report
    beside it. No inline script runs.

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
            f"script-src 'self' '{showcase_mod.COPY_SCRIPT_HASH}'; connect-src 'self'; "
            "frame-src 'self'; frame-ancestors 'self'",
        )
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def build_app() -> Starlette:
    """Build the viewer app. Auth binds every route except the probes.

    The JSON routes of ``viewer_api`` come after the probes, and the mount of
    the React app is the last route, so every explicit route wins over it.
    The wiki, the attachments and ``/`` are routes of the app, so they are
    not here: with no build in the image, they are 404 and the pages the
    server still renders answer as always.
    """
    routes: list[BaseRoute] = [
        Route("/healthz", healthz),
        Route("/readyz", readyz),
        *viewer_api.routes(),
        Route("/style.css", _require_auth(style)),
        Route("/ui/{name}", _require_auth(ui_file)),
        Route("/assignments", _require_auth(assignments_list)),
        Route("/assignments/saved", _require_auth(saved_list)),
        Route("/assignments/saved/{id:int}", _require_auth(saved_page)),
        Route("/assignments/{id:int}", _require_auth(assignment_page)),
        Route(
            "/assignments/{id:int}/comments",
            _require_auth(comment_on_assignment),
            methods=["POST"],
        ),
        Route(
            "/assignments/{id:int}/comments/{cid:int}/delete",
            _require_auth(comment_on_assignment),
            methods=["POST"],
        ),
        Route(
            "/assignments/saved/{id:int}/comments",
            _require_auth(comment_on_saved),
            methods=["POST"],
        ),
        Route(
            "/assignments/saved/{id:int}/comments/{cid:int}/delete",
            _require_auth(comment_on_saved),
            methods=["POST"],
        ),
        Route("/documents", _require_auth(documents_list)),
        # The JSON route comes first, so the page route does not take
        # ``<slug>.json``. A slug that ends in ``.json`` is not a
        # document, so the two can never name the same file.
        Route("/documents/{owner}/{slug}.json", _require_auth(document_meta)),
        Route("/documents/{owner}/{slug}", _require_auth(document_page)),
        Route("/documents/{owner}/{slug}/artifact", _require_auth(document_artifact)),
        Route(
            "/documents/{owner}/{slug}/comments",
            _require_auth(document_comment),
            methods=["POST"],
        ),
        Route(
            "/documents/{owner}/{slug}/comments/{cid}/delete",
            _require_auth(document_comment),
            methods=["POST"],
        ),
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
