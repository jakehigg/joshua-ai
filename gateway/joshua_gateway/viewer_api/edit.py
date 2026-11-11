"""The JSON routes of the one editor: an edit, a create, a save, a delete.

A member has three writes in the viewer, and they are all here:

- ``GET /api/v1/edit?path=<wiki-relative path>`` gives the editor the text of
  one wiki page or journal entry, and the CSRF token of its save.
- ``GET /api/v1/new?folder=<wiki folder>`` gives the editor an empty page in a
  folder, and the CSRF token of its save.
- ``POST /api/v1/save`` writes the page, the entry, or the new page, and
  answers where to go.
- ``POST /api/v1/delete`` moves a wiki page under ``wiki/.trash/``. It is
  never a hard delete.

``viewer_edit`` says how the path decides the shape of the editor and of the
saved file. Every write goes through the same checks: the member role, the
CSRF token of the scope, the ``Origin`` header, the resolver of the files MCP
with ``write=True``, the size cap, the atomic write, and one commit of the
wiki. A refusal is a status with an ``error`` string, so the editor shows what
the viewer said: 403 for a guest, a stale token, or a cross-site origin, 404
for a path that is not there, 409 for a name that is already taken, and 400
for a body the person can fix.

The scope of a token is the same string the delete of a page already uses, so
a token minted by one route is accepted by the other: ``wiki/<rel>`` for an
edit, ``new:<folder>`` for a create, and the root-relative path itself for a
delete.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import UTC, date, datetime
from json import JSONDecodeError
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from joshua_shared import config
from joshua_shared.config import Person
from joshua_shared.layout import journal_day_from_path, journal_root
from joshua_shared.log import get_logger
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway import viewer_edit as edit
from joshua_gateway import viewer_journal as journal
from joshua_gateway import viewer_wiki as wiki_mod
from joshua_gateway.files_mcp.paths import PathError, Root, resolve
from joshua_gateway.files_mcp.server import MAX_BYTES, _atomic_write, data_root

logger = get_logger("viewer")

# The per-process secret for the CSRF token of a write. A fresh value each
# start is fine; a token from an earlier process fails and the person reloads
# the editor.
_CSRF_SECRET = secrets.token_bytes(32)

# The timestamp folder of the trash.
_TRASH_STAMP = "%Y%m%dT%H%M%SZ"

# What a refusal says, by its status.
_REFUSED = {
    403: "forbidden",
    404: "not found",
    409: "A page with that name is already there.",
}

# The messages of a save the person can fix.
NOT_JSON = "The save is not a JSON object."
TOO_BIG = "The page exceeds 256 KB."


# -- the addresses of the editor --------------------------------------------


def edit_url(rel: str) -> str:
    """The address of the editor of one wiki page: ``rel`` is wiki-relative."""
    return "/edit/" + quote(rel)


def new_url(folder: str) -> str:
    """The address of the editor of a new page in a wiki folder."""
    return "/new?folder=" + quote(folder) if folder else "/new"


# -- the checks of a write --------------------------------------------------


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


def _edit_target(person: Person, rel: str, *, create: bool) -> tuple[Root, Path, date | None] | int:
    """Resolve a wiki-relative path for a write, or the status that refuses it.

    The third value is the journal day when the path is an entry under a day
    folder. A path under the journal that is not such an entry, and a new page
    anywhere under the journal, are refused: the journal has one writer.
    """
    from joshua_gateway import viewer  # ``viewer`` imports this package

    if person.role != "member":
        return 403
    try:
        root, abs_path = resolve("wiki/" + rel, viewer._roots_for(person), write=True)
    except PathError:
        return 404
    if root.name != "wiki" or abs_path.suffix != ".md":
        return 404
    day = journal_day_from_path(abs_path, data_root())
    in_journal = (
        abs_path == journal_root(data_root()) or journal_root(data_root()) in abs_path.parents
    )
    if create:
        if in_journal:
            return 403
        if abs_path.exists():
            return 409
    else:
        if not abs_path.is_file():
            return 404
        if in_journal and day is None:
            return 404
    return root, abs_path, day


def _under_wiki(root: Root, abs_path: Path) -> str:
    return abs_path.relative_to(root.base).as_posix()


# -- the writes on disk -----------------------------------------------------


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


# -- the answers ------------------------------------------------------------


def _answer(body: dict[str, Any], status: int = 200) -> JSONResponse:
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse(body, status_code=status, headers=viewer._NO_STORE)


def _refused(status: int) -> JSONResponse:
    """The refusal of one write: the status, and what it says."""
    return _answer({"error": _REFUSED[status]}, status)


def _bad(message: str) -> JSONResponse:
    """A save the person can fix: the message the editor shows."""
    return _answer({"error": message}, 400)


async def _fields(request: Request) -> dict[str, Any] | None:
    """The JSON object of a write, or None when the body is not one."""
    try:
        body = await request.json()
    except (JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _field(fields: dict[str, Any], name: str) -> str:
    """One field of the body as text. A value that is not a string is "" ."""
    value = fields.get(name)
    return value if isinstance(value, str) else ""


# -- the routes -------------------------------------------------------------


async def edit_page(request: Request, person: Person) -> JSONResponse:
    """The text of one wiki page or journal entry, for the editor.

    A journal entry also carries the people it names, and its subtitle says
    the day, who wrote it, and the path. A wiki page carries its frontmatter
    as raw text, which the save keeps and the editor shows above the text.
    """
    from joshua_gateway import viewer  # ``viewer`` imports this package

    rel = request.query_params.get("path") or ""
    target = _edit_target(person, rel, create=False)
    if isinstance(target, int):
        return _refused(target)
    root, abs_path, day = target
    text = viewer._read_text(abs_path)
    if text is None:
        return _refused(404)
    csrf = _csrf_token(person.id, "wiki/" + rel)
    if day is not None:
        entry = journal.parse_entry(rel, day, text)
        return _answer(
            {
                "kind": "journal",
                "rel": rel,
                "title": entry.title,
                "subtitle": f"{day.strftime('%A %-d %B %Y')} · {entry.source} · {rel}",
                "body": entry.body,
                "kept_frontmatter": None,
                "people": list(entry.people),
                "cancel_url": entry.day_url,
                "csrf": csrf,
            }
        )
    front, body = edit.split_raw_frontmatter(text)
    return _answer(
        {
            "kind": "wiki",
            "rel": rel,
            "title": wiki_mod.page_title(text, wiki_mod.stem_title(abs_path.stem)),
            "subtitle": rel,
            "body": body,
            "kept_frontmatter": front or None,
            "people": None,
            "cancel_url": f"/wiki/{rel}",
            "csrf": csrf,
        }
    )


async def new_page(request: Request, person: Person) -> JSONResponse:
    """The empty editor of a new page in a wiki folder, and its token."""
    from joshua_gateway import viewer  # ``viewer`` imports this package

    if person.role != "member":
        return _refused(403)
    folder = (request.query_params.get("folder") or "").strip("/")
    try:
        _, abs_dir = resolve(
            "wiki/" + folder if folder else "wiki", viewer._roots_for(person), write=False
        )
    except PathError:
        return _refused(404)
    if not abs_dir.is_dir():
        return _refused(404)
    return _answer(
        {
            "folder": folder,
            "cancel_url": f"/wiki/{folder}/" if folder else "/wiki/",
            "csrf": _csrf_token(person.id, "new:" + folder),
        }
    )


async def save(request: Request, person: Person) -> JSONResponse:
    """Write a page, an entry, or a new page. One path for all three: the
    CSRF token, the origin, the target, the composed text, the size cap, the
    atomic write, the commit.

    ``folder`` in the body means a create; ``path`` means an edit. The answer
    is where to go: the page itself, or the day of the entry.
    """
    from joshua_gateway import viewer  # ``viewer`` imports this package

    if person.role != "member":
        return _refused(403)
    fields = await _fields(request)
    if fields is None:
        return _bad(NOT_JSON)
    token = _field(fields, "csrf")
    body = _field(fields, "body")
    create = "folder" in fields
    if create:
        folder = _field(fields, "folder").strip("/")
        if not _csrf_valid(person.id, "new:" + folder, token) or not _origin_ok(request):
            return _refused(403)
        try:
            rel = edit.new_page_rel(folder, _field(fields, "name"))
        except edit.EditError as exc:
            return _bad(str(exc))
    else:
        rel = _field(fields, "path")
        if not _csrf_valid(person.id, "wiki/" + rel, token) or not _origin_ok(request):
            return _refused(403)
    target = _edit_target(person, rel, create=create)
    if isinstance(target, int):
        return _refused(target)
    root, abs_path, day = target
    try:
        if create:
            data = edit.compose_new(body)
            after = f"/wiki/{rel}"
            what = "new"
        else:
            text = viewer._read_text(abs_path)
            if text is None:
                return _refused(404)
            if day is not None:
                data = edit.compose_journal(text, rel, day, _field(fields, "people"), body)
                after = f"{journal.day_url(day)}#{abs_path.stem}"
            else:
                data = edit.compose_wiki(text, body)
                after = f"/wiki/{rel}"
            what = "edit"
    except edit.EditError as exc:
        return _bad(str(exc))
    raw = data.encode("utf-8")
    if len(raw) > MAX_BYTES:
        return _bad(TOO_BIG)
    _atomic_write(abs_path, raw)
    _commit_write(root, abs_path, what)
    logger.info({"message": "viewer write", "root": "wiki", "what": what})
    return _answer({"ok": True, "url": after})


async def delete(request: Request, person: Person) -> JSONResponse:
    """Move one wiki page to the trash of the wiki, and say to go home.

    The path is root-relative, as the page carries it, and only the wiki root
    is a delete: an attachment and a shared file are read-only.
    """
    from joshua_gateway import viewer  # ``viewer`` imports this package

    if person.role != "member":
        return _refused(403)
    fields = await _fields(request)
    if fields is None:
        return _bad(NOT_JSON)
    path = _field(fields, "path")
    if not _csrf_valid(person.id, path, _field(fields, "csrf")) or not _origin_ok(request):
        return _refused(403)
    try:
        root, abs_path = resolve(path, viewer._roots_for(person), write=False)
    except PathError:
        return _refused(404)
    if root.name != "wiki":
        return _refused(403)
    if not abs_path.is_file():
        return _refused(404)
    _move_to_trash(root, abs_path)
    logger.info({"message": "viewer delete", "root": "wiki"})
    return _answer({"ok": True, "url": "/"})
