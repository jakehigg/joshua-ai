"""Web viewer for the wiki, the journal, and a person's own attachments.

A person opens a browser, signs in with HTTP basic, and reads the one wiki
(which carries Joshua's own journal and its profile of each person), their own
attachments, and the shared attachments. ``/journal`` shows the journal as a
feed, newest day first, because the wiki tree is the wrong shape for reading
what happened. The agent never calls the viewer. The viewer calls core for two
things, when ``CORE_URL`` and ``JOSHUA_TOKEN_VIEWER`` are set: ``/search`` asks
core to search by meaning (``viewer_search``), and ``/usage`` shows the tokens
of each group by day (``viewer_usage``) under the subscription limits
(``viewer_limits``). The limits need ``CORE_URL`` only, because core's
``GET /v1/limits`` has no bearer. ``/assignments`` shows the assignments of
the person who signed in, read from the assignments manager when
``ASSIGNMENTS_URL`` and ``JOSHUA_TOKEN_VIEWER`` are set
(``viewer_assignments``). ``/documents`` shows the documents of the member
who signed in and the shared ones, read from ``/data/documents``
(``viewer_documents``); a guest has no such section. The viewer reads the data volume
through the same resolver as the files MCP (``files_mcp.paths``), so the two
never drift on who reads what.

A member has two write actions, both human actions in a browser that the
agent never gets: a delete, which moves a wiki page to the trash under
``wiki/.trash/`` and is never a hard delete, and an edit of a journal entry's
text and people. A guest reads. A person can also comment on a line of the
definition of their own assignment: the viewer sends the comment to the
assignments manager as that person, and writes nothing itself. A member can
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

import asyncio
import base64
import binascii
import hashlib
import hmac
import os
import secrets
import sys
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, date, datetime, tzinfo
from functools import cache
from html import escape
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import bcrypt
import uvicorn
import yaml
from joshua_shared import config, log
from joshua_shared.config import Person
from joshua_shared.layout import is_hidden, journal_day_from_path, journal_root
from joshua_shared.layout import wiki_root as wiki_dir
from joshua_shared.log import get_logger, install_healthcheck_filter
from markdown_it import MarkdownIt
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from joshua_gateway import viewer_assignments as assignments_mod
from joshua_gateway import viewer_comments as comments_mod
from joshua_gateway import viewer_docstore as docstore
from joshua_gateway import viewer_documents as documents_mod
from joshua_gateway import viewer_edit as edit
from joshua_gateway import viewer_journal as journal
from joshua_gateway import viewer_limits as limits_mod
from joshua_gateway import viewer_links as links_mod
from joshua_gateway import viewer_search as search_mod
from joshua_gateway import viewer_showcase as showcase_mod
from joshua_gateway import viewer_usage as usage_mod
from joshua_gateway import viewer_wiki as wiki
from joshua_gateway.files_mcp.paths import PathError, Root, resolve
from joshua_gateway.files_mcp.server import IMAGE_MIME, MAX_BYTES, _atomic_write, data_root

logger = get_logger("viewer")

# The basic-auth realm and the trash timestamp format.
REALM = "joshua viewer"
_TRASH_STAMP = "%Y%m%dT%H%M%SZ"

# Where a viewer password can come from, besides the entry in ``viewer.users``.
# ``VIEWER_PASSWORDS`` carries every one of them in a single variable, so a
# tracked file can pass them all through and name no person.
VIEWER_PASSWORDS_ENV = "VIEWER_PASSWORDS"
VIEWER_PW_PREFIX = "VIEWER_PW_"

# The person's home page shows at most this many recent attachments; search
# returns at most this many hits.
HOME_ATTACHMENTS_MAX = 30
SEARCH_MAX_RESULTS = 50

# The metadata file a channels-stored attachment carries: ``<file>.meta.json``. Never
# a piece of content on its own, so a listing hides it.
_META_SUFFIX = ".meta.json"

# The per-process secret for the delete CSRF token. A fresh value each start is
# fine; a form from an earlier process fails and the person reloads the page.
_CSRF_SECRET = secrets.token_bytes(32)

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

# Which viewer route serves each root, for a search hit link.
_ROUTE_PREFIX = {"wiki": "/wiki/", "shared": "/shared/"}

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
)
# The sections that a guest does not get.
_MEMBER_SECTIONS = frozenset({"documents"})
_SECTION_LABEL = {key: label for key, label, _, _ in _SECTIONS} | {"links": "Links", "me": "You"}
_SEARCH_ICON = _SECTIONS[2][3]
_ME_ICON = '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>'


def _icon(paths: str) -> str:
    return f'<svg class="j-icon" viewBox="0 0 24 24" aria-hidden="true">{paths}</svg>'


def _current(key: str, section: str) -> str:
    return ' aria-current="page"' if key == section else ""


def _sections() -> list[tuple[str, str, str, str]]:
    """The sections of the person of this request: a guest gets no Documents."""
    person = _REQUEST_PERSON.get()
    member = person is not None and person.role == "member"
    return [s for s in _SECTIONS if member or s[0] not in _MEMBER_SECTIONS]


def _shell(body: str, *, section: str, query: str) -> str:
    """The page shell: the sidebar on a wide screen; the app bar and the tabs
    on a phone. ``section`` marks the current item. The search field is in the
    app bar, so it shows on every screen."""
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
    content = "j-content wide" if section == "usage" else "j-content"
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
        f'<input type="search" name="q" value="{escape(query, quote=True)}" '
        'placeholder="Search" aria-label="Search"></label></form>'
        "</header>"
        f'<nav class="j-tabs mobile-only" aria-label="Sections">{tabs}</nav>'
        f'<main class="{content}">{body}</main>'
        "</div></div>"
    )


def _page(
    title: str,
    body: str,
    *,
    status: int = 200,
    nav: bool = True,
    section: str = "",
    query: str = "",
    head: str = "",
) -> HTMLResponse:
    """A full page. ``section`` is the key of the current section in the
    navigation (``wiki``, ``journal``, ``search``, ``usage``,
    ``assignments``, ``documents``), or "" for the home page. ``nav`` False gives the bare
    content with no navigation. ``head`` is more HTML for the ``<head>``,
    such as a ``<meta>`` refresh."""
    main = (
        _shell(body, section=section, query=query)
        if nav
        else f'<main class="j-content">{body}</main>'
    )
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


def _safe_filename(name: str) -> str:
    return name.replace('"', "").replace("\\", "").replace("\r", "").replace("\n", "")


def _serve(roots: dict[str, Root], rel: str, person: Person, *, allow_delete: bool) -> Response:
    """Serve one file by its root-relative path.

    An image renders inline; a markdown file renders as a page; any other type
    downloads with ``Content-Disposition: attachment``. A path that escapes a
    root, or a file that is not there, returns 404.
    """
    try:
        root, abs_path = resolve(rel, roots, write=False)
    except PathError:
        return _not_found()
    if not abs_path.is_file():
        return _not_found()

    mime = IMAGE_MIME.get(abs_path.suffix.lower())
    if mime is not None:
        return Response(abs_path.read_bytes(), media_type=mime)

    if abs_path.suffix.lower() == ".md":
        text = _read_text(abs_path)
        if text is None:
            return _download(abs_path)
        front, body_md = _split_frontmatter(text)
        title = rel
        body = ""
        under_wiki = abs_path.relative_to(root.base).as_posix() if root.name == "wiki" else ""
        if root.name == "wiki":
            # A wiki page gets its title, the crumbs above it, and the other
            # pages of its folder below it.
            title = wiki.page_title(text, wiki.stem_title(abs_path.stem))
            folder_rel = under_wiki.rsplit("/", 1)[0] if "/" in under_wiki else ""
            body += wiki.crumbs_html(root.base, folder_rel, leaf=title)
        rendered = _MD.render(body_md)
        if root.name == "wiki":
            rendered = _wiki_attachment_links(rendered)
        body += wiki.meta_html(front or {}) + f'<article class="j-prose">{rendered}</article>'
        if root.name == "wiki":
            body += wiki.siblings_html(root.base, under_wiki)
        if allow_delete and root.name == "wiki" and person.role == "member":
            body += (
                f'<div class="v-actions">{edit.edit_link(under_wiki)}'
                f"{_delete_form(person, rel)}</div>"
            )
        return _page(title, body, section="wiki" if root.name == "wiki" else "")

    return _download(abs_path)


def _wiki_attachment_links(html: str) -> str:
    """Point a page's `/attachments/…` links at the wiki folder of this viewer.

    A page stores the link the way a wiki frontend that serves the wiki at its
    root reads it: `/attachments/YYYY/MM/name`. This viewer serves the wiki
    under `/wiki/`, and `/attachments/` here is the person's own files, so the
    link is rewritten as the page is rendered. The stored page is not changed.
    """
    return html.replace('src="/attachments/', 'src="/wiki/attachments/').replace(
        'href="/attachments/', 'href="/wiki/attachments/'
    )


def _download(abs_path: Path) -> Response:
    return Response(
        abs_path.read_bytes(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{_safe_filename(abs_path.name)}"'},
    )


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


def _section(title: str, links: list[tuple[str, str]]) -> str:
    if not links:
        return f'<h2 class="j-label">{escape(title)}</h2><p class="j-empty">nothing yet.</p>'
    items = "".join(f'<li><a href="{escape(url)}">{escape(text)}</a></li>' for url, text in links)
    return f'<h2 class="j-label">{escape(title)}</h2><ul class="tree">{items}</ul>'


# -- delete -----------------------------------------------------------------


def _csrf_token(person_id: str, path: str) -> str:
    message = f"{person_id}\n{path}".encode()
    return hmac.new(_CSRF_SECRET, message, hashlib.sha256).hexdigest()


def _csrf_valid(person_id: str, path: str, token: str) -> bool:
    return bool(token) and hmac.compare_digest(_csrf_token(person_id, path), token)


def _origin_ok(request: Request) -> bool:
    """True when the request has no ``Origin`` header or it matches the host."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    return urlparse(origin).netloc == request.headers.get("host", "")


def _delete_form(person: Person, rel: str) -> str:
    token = _csrf_token(person.id, rel)
    return (
        '<form class="delete" action="/delete" method="post">'
        f'<input type="hidden" name="path" value="{escape(rel, quote=True)}">'
        f'<input type="hidden" name="csrf" value="{escape(token, quote=True)}">'
        '<button class="j-btn sm danger" type=submit>move this page to trash</button></form>'
    )


def _move_to_trash(root: Root, abs_path: Path) -> Path:
    """Move a wiki file under ``wiki/.trash/<UTC timestamp>/`` and return the new
    path. The timestamp directory keeps the original tree."""
    under_wiki = abs_path.relative_to(root.base)
    stamp = datetime.now(UTC).strftime(_TRASH_STAMP)
    dest = root.base / ".trash" / stamp / under_wiki
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(abs_path, dest)
    _commit_trash(root, abs_path)
    return dest


def _commit_trash(root: Root, abs_path: Path) -> None:
    """Commit the delete ``_move_to_trash`` just made, when ``wiki.git`` is
    enabled. ``.trash/`` is gitignored, so the commit records the removal
    only, never the trashed copy. A failure here is a WARNING; the delete
    already happened."""
    try:
        from joshua_shared import wikigit

        if not wikigit.is_enabled(config.load()):
            return
        rel = abs_path.relative_to(root.base).as_posix()
        wikigit.commit(root.base, [abs_path], f"viewer: trash {rel}")
    except Exception as exc:  # noqa: BLE001 — a commit must never fail the delete
        logger.warning({"message": "wiki commit failed", "error": str(exc)})


# -- routes -----------------------------------------------------------------


async def root(request: Request, person: Person) -> Response:
    """``/`` opens the wiki."""
    return RedirectResponse("/wiki/", status_code=302)


async def index(request: Request, person: Person) -> Response:
    """``/me``: the person's own page, with their profile link, their recent
    attachments and the wiki tree."""
    roots = _roots_for(person)
    anonymous = person.id == ANONYMOUS_PERSON_ID
    body = [f'<h1 class="j-doc-title">{escape(person.name)}</h1>']
    if anonymous:
        body.append('<p class="j-meta"><a href="/journal">journal</a></p>')
    else:
        profile_url = f"/wiki/people/{person.id}.md"
        body.append(
            f'<p class="j-meta"><a href="{escape(profile_url, quote=True)}">profile</a> · '
            '<a href="/journal">journal</a></p>'
        )
        # The anonymous identity has no attachments folder on disk: there is
        # no such person, so the section has nothing to show and is omitted.
        body.append(
            _section(
                "attachments",
                [
                    (f"/attachments/{rel}", rel)
                    for rel in _list_attachments(roots["attachments"])[:HOME_ATTACHMENTS_MAX]
                ],
            )
        )
    tree = wiki.build_tree(roots["wiki"].base)
    body.append('<h2 class="j-label">wiki</h2>')
    body.append(wiki.tree_html(tree) if tree else '<p class="j-empty">nothing yet.</p>')
    return _page(person.name, "".join(body), section="me")


async def wiki_page(request: Request, person: Person) -> Response:
    """A wiki page, or a folder page for a path that ends in ``/`` or names
    a directory. ``/wiki/`` is the root folder: the front page and the tree."""
    path = request.path_params["path"]
    roots = _roots_for(person)
    if path == "" or path.endswith("/"):
        return _folder_page(roots, path.rstrip("/"), person)
    rel = "wiki/" + path
    try:
        _, abs_path = resolve(rel, roots, write=False)
    except PathError:
        return _not_found()
    if abs_path.is_dir():
        return RedirectResponse(f"/wiki/{path}/", status_code=302)
    return _serve(roots, rel, person, allow_delete=True)


async def wiki_root(request: Request, person: Person) -> Response:
    return RedirectResponse("/wiki/", status_code=302)


def _folder_page(roots: dict[str, Root], rel: str, person: Person) -> Response:
    """The page for one folder: its index page's text, then what is in it."""
    try:
        root, abs_path = resolve("wiki/" + rel if rel else "wiki", roots, write=False)
    except PathError:
        return _not_found()
    if not abs_path.is_dir():
        return _not_found()
    folder = wiki.build_tree(root.base, rel)
    if folder is None:
        return _not_found()
    index_html = ""
    if folder.index is not None:
        text = _read_text(root.base / folder.index.rel) or ""
        _, body_md = _split_frontmatter(text)
        _, body_md = wiki.first_heading(body_md)  # the folder title is the h1
        index_html = wiki.index_links(_MD.render(body_md), folder.index.rel)
        index_html = _wiki_attachment_links(index_html)
    body = wiki.crumbs_html(root.base, rel, leaf=None) if rel else ""
    body += wiki.folder_html(folder, index_html)
    if person.role == "member":
        body += f'<div class="v-actions">{edit.new_link(rel)}</div>'
    return _page(folder.title, body, section="wiki")


async def shared_page(request: Request, person: Person) -> Response:
    rel = "shared/" + request.path_params["path"]
    return _serve(_roots_for(person), rel, person, allow_delete=False)


async def attachments(request: Request, person: Person) -> Response:
    rel = "attachments/" + request.path_params["path"]
    return _serve(_roots_for(person), rel, person, allow_delete=False)


async def search(request: Request, person: Person) -> Response:
    """Search by meaning through core, when core is configured, and by words.

    The words section always shows, so the page still works when core is
    down or not configured."""
    query = (request.query_params.get("q") or "").strip()
    if not query:
        return _page(
            "search",
            '<h1 class="j-doc-title">search</h1><p class="j-empty">Enter a query.</p>',
            section="search",
        )
    meaning = ""
    core = search_mod.from_env(os.environ)
    if core is not None:
        reader = person.id if person.role == "member" else None
        meaning = search_mod.meaning_html(await core.search(query, person=reader))
    hits = _search(_roots_for(person), query)
    if not hits:
        words = f'<p class="j-empty">No match for {escape(query)}.</p>'
    else:
        words = search_mod.results_html([(url, label, "", snippet) for url, label, snippet in hits])
    if meaning:
        words = f'<section class="words"><h2>By words</h2>{words}</section>'
    return _page(
        "search",
        f'<h1 class="j-doc-title">search</h1>{meaning}{words}',
        section="search",
        query=query,
    )


async def usage(request: Request, person: Person) -> Response:
    """The subscription limits and the tokens of each group by day, from core.
    The two calls run at the same time. The page answers 200 also when core is
    not configured or does not answer, and says so."""
    days = usage_mod.parse_days(request.query_params.get("days"))
    core = usage_mod.from_env(os.environ)
    limits_core = limits_mod.from_env(os.environ)
    body, limits = await asyncio.gather(
        core.fetch(days) if core is not None else _none(),
        limits_core.fetch() if limits_core is not None else _none(),
    )
    tz = limits_mod.zone(config.load().timezone)
    section = limits_mod.section_html(
        limits, configured=limits_core is not None, tz=tz, now=_now(tz)
    )
    return _page(
        "usage",
        usage_mod.page_html(body, days=days, configured=core is not None, limits=section),
        section="usage",
    )


async def _none() -> None:
    return None


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


async def _comment_write(request: Request, person: Person, kind: str) -> Response:
    """Send one comment write of the person to the manager, then go back to the line.

    The form carries the CSRF token of the definition, and a cross-site
    ``Origin`` is refused. The manager writes the comment as the person who
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
    comment_id = request.path_params.get("cid")
    if comment_id is not None:
        written = await client.delete_comment(person.id, kind, parent_id, int(comment_id))
        anchor = "definition"
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
        )
        anchor = f"L{line_no}"
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
    logger.info({"message": "viewer comment", "kind": kind, "delete": comment_id is not None})
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
        line_no = 0
    lines = doc.lines
    if not 1 <= line_no <= len(lines):
        return _comment_refused(doc, "The line is not in the document.")
    if not body:
        return _comment_refused(doc, "The comment is empty.")
    if len(body) > comments_mod.MAX_COMMENT:
        return _comment_refused(doc, f"The comment exceeds {comments_mod.MAX_COMMENT} characters.")
    line_text = str(form.get("line_text", "")) or lines[line_no - 1]
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


def _search(roots: dict[str, Root], query: str) -> list[tuple[str, str, str]]:
    """Substring search over the markdown the person can read. One hit per file,
    newest roots first. This is not semantic search."""
    needle = query.lower()
    hits: list[tuple[str, str, str]] = []
    for name, root in roots.items():
        if name == "attachments":
            continue
        for abs_path, rel in _iter_markdown(root):
            text = _read_text(abs_path)
            if text is None:
                continue
            for line in text.splitlines():
                if needle in line.lower():
                    label = _search_label(name, rel)
                    if name == "wiki":
                        label = wiki.page_title(text, wiki.stem_title(abs_path.stem))
                    hits.append((_search_url(name, rel), label, line.strip()[:200]))
                    break
            if len(hits) >= SEARCH_MAX_RESULTS:
                return hits
    return hits


def _search_url(root_name: str, rel: str) -> str:
    return f"{_ROUTE_PREFIX[root_name]}{rel}"


def _search_label(root_name: str, rel: str) -> str:
    return f"{root_name}/{rel}"


async def delete(request: Request, person: Person) -> Response:
    if person.role != "member":
        return _forbidden()
    form = await request.form()
    path = str(form.get("path", ""))
    token = str(form.get("csrf", ""))
    if not _csrf_valid(person.id, path, token) or not _origin_ok(request):
        return _forbidden()
    roots = _roots_for(person)
    try:
        root, abs_path = resolve(path, roots, write=False)
    except PathError:
        return _not_found()
    if root.name != "wiki":
        return _forbidden()
    if not abs_path.is_file():
        return _not_found()
    _move_to_trash(root, abs_path)
    logger.info({"message": "viewer delete", "root": "wiki"})
    return RedirectResponse("/", status_code=303)


# -- journal ----------------------------------------------------------------


def _people_list() -> list[tuple[str, str]]:
    """``(id, name)`` for every person, for the filter and the chips."""
    return [(p.id, p.name) for p in config.load().people]


async def journal_feed(request: Request, person: Person) -> Response:
    """The journal for one month, newest day first. ``month=YYYY-MM`` picks the
    month; the default is the newest month with an entry. ``person=<id>``
    keeps only the entries that name that person."""
    entries = journal.load_entries(data_root())
    person_id = (request.query_params.get("person") or "").strip() or None
    if person_id is not None and person_id not in {pid for pid, _ in _people_list()}:
        person_id = None
    scoped = journal.for_person(entries, person_id)
    available = journal.months(scoped)
    month = journal.parse_month(request.query_params.get("month"))
    if month is None:
        today = datetime.now(UTC).date()
        month = available[0] if available else (today.year, today.month)
    body = journal.feed_html(
        journal.for_month(scoped, month),
        month=month,
        available=available,
        people=_people_list(),
        person_id=person_id,
        can_edit=person.role == "member",
    )
    return _page("journal", body, section="journal")


async def journal_day(request: Request, person: Person) -> Response:
    """One day of the journal, with the nearest older and newer day that
    holds an entry."""
    try:
        day = date(
            int(request.path_params["year"]),
            int(request.path_params["month"]),
            int(request.path_params["day"]),
        )
    except ValueError:
        return _not_found()
    entries = journal.load_entries(data_root())
    all_days = journal.days(entries)  # newest first
    older = next((d for d in all_days if d < day), None)
    newer = next((d for d in reversed(all_days) if d > day), None)
    body = journal.day_html(
        day,
        journal.for_day(entries, day),
        older=older,
        newer=newer,
        people=_people_list(),
        can_edit=person.role == "member",
    )
    return _page(day.isoformat(), body, section="journal")


# -- edit -------------------------------------------------------------------
#
# One editor for a wiki page and a journal entry. ``viewer_edit`` says how the
# path decides the shape of the form and of the saved file. Every write here
# goes through ``_edit_target`` and ``save``: the same member check, the same
# resolver with ``write=True``, the same CSRF token and origin check, the same
# atomic write and commit.


def _edit_target(
    person: Person, rel: str, *, create: bool
) -> tuple[Root, Path, date | None] | Response:
    """Resolve a wiki-relative path for a write, or the response that refuses
    it. The third value is the journal day when the path is an entry under a
    day folder. A path under the journal that is not such an entry, and a
    new page anywhere under the journal, are refused: the journal has one
    writer."""
    if person.role != "member":
        return _forbidden()
    try:
        root, abs_path = resolve("wiki/" + rel, _roots_for(person), write=True)
    except PathError:
        return _not_found()
    if root.name != "wiki" or abs_path.suffix != ".md":
        return _not_found()
    day = journal_day_from_path(abs_path, data_root())
    in_journal = (
        abs_path == journal_root(data_root()) or journal_root(data_root()) in abs_path.parents
    )
    if create:
        if in_journal:
            return _forbidden()
        if abs_path.exists():
            return _page(
                "exists",
                '<h1 class="j-doc-title">exists</h1>'
                '<p class="j-error">A page with that name is already there.</p>',
                status=409,
                section="wiki",
            )
    else:
        if not abs_path.is_file():
            return _not_found()
        if in_journal and day is None:
            return _not_found()
    return root, abs_path, day


def _under_wiki(root: Root, abs_path: Path) -> str:
    return abs_path.relative_to(root.base).as_posix()


async def edit_page(request: Request, person: Person) -> Response:
    """The edit form for a wiki page or a journal entry."""
    rel = request.path_params["path"]
    target = _edit_target(person, rel, create=False)
    if isinstance(target, Response):
        return target
    root, abs_path, day = target
    text = _read_text(abs_path)
    if text is None:
        return _not_found()
    csrf = _csrf_token(person.id, "wiki/" + rel)
    if day is not None:
        entry = journal.parse_entry(rel, day, text)
        form = edit.form_html(
            title=f"edit: {entry.title}",
            subtitle=f"{day.strftime('%A %-d %B %Y')} · {entry.source} · {rel}",
            csrf=csrf,
            path=rel,
            body=entry.body,
            people=entry.people,
            cancel_url=entry.day_url,
        )
        return _page(f"edit {entry.title}", form, section="journal")
    front, body = edit.split_raw_frontmatter(text)
    title = wiki.page_title(text, wiki.stem_title(abs_path.stem))
    form = edit.form_html(
        title=f"edit: {title}",
        subtitle=rel,
        csrf=csrf,
        path=rel,
        body=body,
        kept_front=front,
        cancel_url=f"/wiki/{rel}",
    )
    return _page(f"edit {title}", form, section="wiki")


async def new_page(request: Request, person: Person) -> Response:
    """The form for a new wiki page in a folder."""
    if person.role != "member":
        return _forbidden()
    folder = (request.query_params.get("folder") or "").strip("/")
    roots = _roots_for(person)
    try:
        _, abs_dir = resolve("wiki/" + folder if folder else "wiki", roots, write=False)
    except PathError:
        return _not_found()
    if not abs_dir.is_dir():
        return _not_found()
    return _page("new page", _new_form(person, folder, name="", body="", error=""), section="wiki")


def _new_form(person: Person, folder: str, *, name: str, body: str, error: str) -> str:
    return edit.form_html(
        title="new page",
        subtitle=f"in {folder or 'the wiki'}",
        csrf=_csrf_token(person.id, "new:" + folder),
        folder=folder,
        name=name,
        body=body,
        error=error,
        cancel_url=f"/wiki/{folder}/" if folder else "/wiki/",
    )


async def save(request: Request, person: Person) -> Response:
    """Write a page or an entry from the form. One path for both, and for a
    new page: the CSRF token, the origin, the target, the composed text, the
    size cap, the atomic write, the commit."""
    if person.role != "member":
        return _forbidden()
    form = await request.form()
    token = str(form.get("csrf", ""))
    body = str(form.get("body", ""))
    create = "folder" in form
    if create:
        folder = str(form.get("folder", "")).strip("/")
        if not _csrf_valid(person.id, "new:" + folder, token) or not _origin_ok(request):
            return _forbidden()
        try:
            rel = edit.new_page_rel(folder, str(form.get("name", "")))
        except edit.EditError as exc:
            return _page(
                "new page",
                _new_form(
                    person, folder, name=str(form.get("name", "")), body=body, error=str(exc)
                ),
                status=400,
                section="wiki",
            )
    else:
        rel = str(form.get("path", ""))
        if not _csrf_valid(person.id, "wiki/" + rel, token) or not _origin_ok(request):
            return _forbidden()
    target = _edit_target(person, rel, create=create)
    if isinstance(target, Response):
        return target
    root, abs_path, day = target
    try:
        if create:
            data = edit.compose_new(body)
            after = f"/wiki/{rel}"
            what = "new"
        elif day is not None:
            text = _read_text(abs_path)
            if text is None:
                return _not_found()
            data = edit.compose_journal(text, rel, day, str(form.get("people", "")), body)
            after = f"{journal.day_url(day)}#{abs_path.stem}"
            what = "edit"
        else:
            text = _read_text(abs_path)
            if text is None:
                return _not_found()
            data = edit.compose_wiki(text, body)
            after = f"/wiki/{rel}"
            what = "edit"
    except edit.EditError as exc:
        return _page(
            "edit",
            f'<h1 class="j-doc-title">edit</h1><p class="j-error">{escape(str(exc))}</p>',
            status=400,
            section="journal" if day is not None else "wiki",
        )
    raw = data.encode("utf-8")
    if len(raw) > MAX_BYTES:
        return _page(
            "edit",
            '<h1 class="j-doc-title">edit</h1><p class="j-error">The page exceeds 256 KB.</p>',
            status=400,
            section="journal" if day is not None else "wiki",
        )
    _atomic_write(abs_path, raw)
    _commit_write(root, abs_path, what)
    logger.info({"message": "viewer write", "root": "wiki", "what": what})
    return RedirectResponse(after, status_code=303)


def _commit_write(root: Root, abs_path: Path, what: str) -> None:
    """Commit a write when ``wiki.git`` is on. A failure is a WARNING; the
    write already happened."""
    try:
        from joshua_shared import wikigit

        if not wikigit.is_enabled(config.load()):
            return
        wikigit.commit(root.base, [abs_path], f"viewer: {what} {_under_wiki(root, abs_path)}")
    except Exception as exc:  # noqa: BLE001 — a commit must never fail the write
        logger.warning({"message": "wiki commit failed", "error": str(exc)})


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


# -- links ----------------------------------------------------------------

_LINKS_CSRF = "links"


def _links_body(person: Person, links: list[links_mod.Link], *, error: str = "") -> str:
    body = [
        '<h1 class="j-doc-title">Links</h1>',
        '<p class="j-muted">Links to the tools you use beside Joshua, such as its addons. '
        "Each one opens in a new tab.</p>",
        links_mod.list_html(links),
    ]
    if person.role == "member":
        body.append('<h2 class="j-label">Edit links</h2>')
        body.append(
            links_mod.form_html(links, csrf=_csrf_token(person.id, _LINKS_CSRF), error=error)
        )
    return "".join(body)


async def links_page(request: Request, person: Person) -> Response:
    """The links. A member also gets the form that edits them."""
    links = links_mod.load(wiki_dir(data_root()))
    return _page("links", _links_body(person, links), section="links")


async def links_save(request: Request, person: Person) -> Response:
    """Replace the links with the rows of the form."""
    if person.role != "member":
        return _forbidden()
    form = await request.form()
    if not _csrf_valid(person.id, _LINKS_CSRF, str(form.get("csrf", ""))) or not _origin_ok(
        request
    ):
        return _forbidden()
    base = wiki_dir(data_root())
    try:
        links = links_mod.from_form(form)
    except links_mod.LinkError as exc:
        return _page(
            "links",
            _links_body(person, links_mod.load(base), error=str(exc)),
            status=400,
            section="links",
        )
    abs_path = links_mod.links_path(base)
    _atomic_write(abs_path, links_mod.dump(links).encode())
    _commit_write(_roots_for(person)["wiki"], abs_path, "links")
    logger.info({"message": "viewer write", "root": "wiki", "what": "links", "links": len(links)})
    return RedirectResponse("/links", status_code=303)


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
    stylesheets from ``'self'`` and its images from ``'self'`` (attachments,
    the logo and the icons). The only script that runs is the copy script of
    a report, allowed by its hash: no other script, inline or from a file,
    runs. The ``srcdoc`` frame of an HTML document gets this same policy.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self'; style-src 'self'; "
            f"script-src '{showcase_mod.COPY_SCRIPT_HASH}'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def build_app() -> Starlette:
    """Build the viewer app. Auth binds every route except the probes."""
    return Starlette(
        middleware=[Middleware(SecurityHeaders)],
        routes=[
            Route("/healthz", healthz),
            Route("/readyz", readyz),
            Route("/style.css", _require_auth(style)),
            Route("/ui/{name}", _require_auth(ui_file)),
            Route("/", _require_auth(root)),
            Route("/me", _require_auth(index)),
            Route("/links", _require_auth(links_page)),
            Route("/links", _require_auth(links_save), methods=["POST"]),
            Route("/wiki", _require_auth(wiki_root)),
            Route("/journal", _require_auth(journal_feed)),
            Route("/journal/{year:int}/{month:int}/{day:int}", _require_auth(journal_day)),
            Route("/edit/{path:path}", _require_auth(edit_page)),
            Route("/new", _require_auth(new_page)),
            Route("/save", _require_auth(save), methods=["POST"]),
            Route("/wiki/{path:path}", _require_auth(wiki_page)),
            Route("/shared/{path:path}", _require_auth(shared_page)),
            Route("/attachments/{path:path}", _require_auth(attachments)),
            Route("/search", _require_auth(search)),
            Route("/usage", _require_auth(usage)),
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
            Route("/documents/{owner}/{slug}", _require_auth(document_page)),
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
            Route("/delete", _require_auth(delete), methods=["POST"]),
        ],
    )


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
