"""Read-only web viewer for the wiki and a person's own attachments.

A person opens a browser, signs in with HTTP basic, and reads the one wiki
(which carries Joshua's own journal and its profile of each person), their own
attachments, and the shared attachments. The viewer never calls core and the
agent never calls the viewer. It reads the data volume through the same
resolver as the files MCP (``files_mcp.paths``), so the two never drift on who
reads what.

The one write action is a delete. A member moves a wiki page to the trash under
``wiki/.trash/``; it is never a hard delete. A guest cannot delete. Deletion
stays a human action in a browser, so the agent gets no way to remove a file.

Start it with ``python -m joshua_gateway.viewer``. It refuses to start when
``viewer.enabled`` is false. Put a reverse proxy in front for TLS; the viewer
serves plain HTTP and binds every route behind basic auth except the probes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import bcrypt
import uvicorn
import yaml
from joshua_shared import config, log
from joshua_shared.config import Person
from joshua_shared.layout import is_hidden
from joshua_shared.log import get_logger, install_healthcheck_filter
from markdown_it import MarkdownIt
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from joshua_gateway.files_mcp.paths import PathError, Root, resolve
from joshua_gateway.files_mcp.server import IMAGE_MIME, data_root

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

# The sidecar a channels-stored attachment carries: ``<file>.meta.json``. Never
# a piece of content on its own, so a listing hides it.
_META_SUFFIX = ".meta.json"

# The per-process secret for the delete CSRF token. A fresh value each start is
# fine; a form from an earlier process fails and the person reloads the page.
_CSRF_SECRET = secrets.token_bytes(32)

# Raw HTML is disabled, so a ``<script>`` in a markdown file renders as text.
_MD = MarkdownIt("commonmark", {"html": False, "linkify": False})

# Which viewer route serves each root, for a search hit link.
_ROUTE_PREFIX = {"wiki": "/wiki/", "shared": "/shared/"}

_CSS = """\
:root { color-scheme: light dark; --fg: #1a1a1a; --bg: #fbfbfb;
  --muted: #666; --line: #ddd; --link: #0b5; --code: #f0f0f0; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e6e6e6; --bg: #16181c; --muted: #9aa0a6;
    --line: #333; --link: #6c9; --code: #22252b; } }
* { box-sizing: border-box; }
body { max-width: 46rem; margin: 0 auto; padding: 1.5rem 1rem;
  font: 16px/1.6 system-ui, sans-serif; color: var(--fg);
  background: var(--bg); }
nav { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap;
  padding-bottom: .75rem; margin-bottom: 1rem;
  border-bottom: 1px solid var(--line); }
nav form { margin-left: auto; }
nav input { padding: .3rem .5rem; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
h1, h2, h3 { line-height: 1.25; }
ul.tree { list-style: none; padding-left: 0; }
ul.tree li { padding: .1rem 0; }
article.md img { max-width: 100%; height: auto; }
article.md pre, article.md code { background: var(--code);
  border-radius: 4px; }
article.md pre { padding: .75rem; overflow-x: auto; }
article.md code { padding: .1rem .3rem; }
header.frontmatter { border: 1px solid var(--line); border-radius: 6px;
  padding: .5rem .75rem; margin-bottom: 1rem; font-size: .9rem;
  color: var(--muted); }
header.frontmatter .k { font-weight: 600; }
form.delete { margin-top: 2rem; padding-top: 1rem;
  border-top: 1px solid var(--line); }
form.delete button { padding: .4rem .75rem; cursor: pointer; }
.snippet { color: var(--muted); }
"""


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


def _authenticate(request: Request) -> Person | None:
    """Return the signed-in person, or None for a missing or wrong credential.

    A wrong user and a wrong password fail the same way. A person named in
    ``viewer.users`` who is no longer in ``people`` cannot sign in.
    """
    creds = _basic_credentials(request)
    if creds is None:
        return None
    user, password = creds
    cfg = config.load()
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


def _require_auth(handler: Any) -> Any:
    """Wrap a handler so it runs only for a signed-in person, else returns 401."""

    async def wrapper(request: Request) -> Response:
        person = _authenticate(request)
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


def _page(title: str, body: str, *, status: int = 200, nav: bool = True) -> HTMLResponse:
    chrome = _NAV if nav else ""
    html = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        '<link rel=stylesheet href="/style.css"></head><body>'
        f"{chrome}<main>{body}</main></body></html>"
    )
    return HTMLResponse(html, status_code=status)


_NAV = (
    '<nav><a href="/">home</a>'
    '<form action="/search" method="get">'
    '<input name="q" placeholder="search" aria-label="search"></form></nav>'
)


def _not_found() -> HTMLResponse:
    return _page("not found", "<h1>not found</h1>", status=404, nav=False)


def _forbidden() -> HTMLResponse:
    return _page("forbidden", "<h1>forbidden</h1>", status=403, nav=False)


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
        body = _frontmatter_html(front) + f'<article class="md">{_MD.render(body_md)}</article>'
        if allow_delete and root.name == "wiki" and person.role == "member":
            body += _delete_form(person, rel)
        return _page(rel, body)

    return _download(abs_path)


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
    first, skipping a dot entry and the ``.meta.json`` sidecar next to a file."""
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
        return f"<h2>{escape(title)}</h2><p class=snippet>nothing yet.</p>"
    items = "".join(f'<li><a href="{escape(url)}">{escape(text)}</a></li>' for url, text in links)
    return f"<h2>{escape(title)}</h2><ul class=tree>{items}</ul>"


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
        "<button type=submit>move this page to trash</button></form>"
    )


def _move_to_trash(root: Root, abs_path: Path) -> Path:
    """Move a wiki file under ``wiki/.trash/<UTC timestamp>/`` and return the new
    path. The timestamp directory keeps the original tree."""
    under_wiki = abs_path.relative_to(root.base)
    stamp = datetime.now(UTC).strftime(_TRASH_STAMP)
    dest = root.base / ".trash" / stamp / under_wiki
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(abs_path, dest)
    return dest


# -- routes -----------------------------------------------------------------


async def index(request: Request, person: Person) -> Response:
    roots = _roots_for(person)
    profile_url = f"/wiki/people/{person.id}.md"
    body = [
        f"<h1>{escape(person.name)}</h1>",
        f'<p><a href="{escape(profile_url, quote=True)}">profile</a></p>',
    ]
    body.append(
        _section(
            "attachments",
            [
                (f"/attachments/{rel}", rel)
                for rel in _list_attachments(roots["attachments"])[:HOME_ATTACHMENTS_MAX]
            ],
        )
    )
    body.append(_section("wiki", [(f"/wiki/{rel}", rel) for rel in _list_markdown(roots["wiki"])]))
    return _page(person.name, "".join(body))


async def wiki_page(request: Request, person: Person) -> Response:
    rel = "wiki/" + request.path_params["path"]
    return _serve(_roots_for(person), rel, person, allow_delete=True)


async def shared_page(request: Request, person: Person) -> Response:
    rel = "shared/" + request.path_params["path"]
    return _serve(_roots_for(person), rel, person, allow_delete=False)


async def attachments(request: Request, person: Person) -> Response:
    rel = "attachments/" + request.path_params["path"]
    return _serve(_roots_for(person), rel, person, allow_delete=False)


async def search(request: Request, person: Person) -> Response:
    query = (request.query_params.get("q") or "").strip()
    if not query:
        return _page("search", "<h1>search</h1><p class=snippet>Enter a query.</p>")
    hits = _search(_roots_for(person), query)
    if not hits:
        return _page("search", f"<h1>search</h1><p>No match for {escape(query)}.</p>")
    items = "".join(
        f'<li><a href="{escape(url)}">{escape(label)}</a> '
        f"<span class=snippet>{escape(snippet)}</span></li>"
        for url, label, snippet in hits
    )
    return _page("search", f"<h1>search</h1><ul class=tree>{items}</ul>")


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
                    hits.append(
                        (_search_url(name, rel), _search_label(name, rel), line.strip()[:200])
                    )
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


async def style(request: Request, person: Person) -> Response:
    return Response(_CSS, media_type="text/css")


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
            "data": data_root().is_dir(),
        }
    )


class SecurityHeaders(BaseHTTPMiddleware):
    """Add the fixed security headers to every response.

    ``default-src 'none'`` blocks every sub-resource; the page loads its one
    stylesheet from ``'self'`` and its images from ``'self'`` (attachments).
    There is no script.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; img-src 'self'; style-src 'self'"
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
            Route("/", _require_auth(index)),
            Route("/wiki/{path:path}", _require_auth(wiki_page)),
            Route("/shared/{path:path}", _require_auth(shared_page)),
            Route("/attachments/{path:path}", _require_auth(attachments)),
            Route("/search", _require_auth(search)),
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
    logger.info({"message": "viewer up", "users": len(cfg.viewer.users)})
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None)


if __name__ == "__main__":
    main()
