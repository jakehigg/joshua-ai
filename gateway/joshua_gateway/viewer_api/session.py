"""``GET /api/v1/session``: who signed in, and the navigation of that person.

The answer holds the person, the sections of the navigation, the chat
address of the person, and the timezone of the instance. The table below is
the one source of the sections on the server, and the app holds the same
table with the icons (``src/shell/sections.ts``), so the member rule and the
person rule stay in one place.

The links of the sidebar are not here: ``GET /api/v1/links`` is the one
source of the list, and the sidebar reads it (``viewer_api.links``).

The sign-in is here too, because it is the session of a person starting and
ending. ``viewer_session`` holds the cookie and the calls to core:

- ``POST /api/v1/session/sign-in`` — ``{person, secret, remember?}``. Core
  checks the secret (``POST /auth/verify``) and the answer carries the
  cookie. A wrong secret, a person who is not on the roster, and one with no
  secret all get the same 401, so the answer says nothing about who exists.
- ``POST /api/v1/session/sign-out`` — clears the cookie. It needs no session,
  so a cookie the viewer no longer accepts can still be thrown away.
- ``POST /api/v1/session/secret`` — ``{secret, new_secret}``: the person who
  signed in changes their own secret, with the current one, through core's
  ``POST /auth/secret``. The answer re-issues the cookie, because the change
  moves the stamp that every outstanding cookie of that person carries.
- ``GET /api/v1/auth-method`` — the method of the instance
  (``viewer.auth.method``) and whether the viewer is open. It needs no
  session, because the sign-in page reads it to know which form to render.
- ``GET /api/v1/sign-in/people`` — for the ``pin`` method: the people core
  says can sign in, with the name and the role of each from the roster. It
  needs no session, and it is 404 for any other method, so no other instance
  renders a roster to a stranger.

The three ``POST`` routes also check the ``Origin`` header, as every other
write of the viewer does.
"""

from __future__ import annotations

import os
from typing import Any

from joshua_shared import config
from joshua_shared.config import Person, is_member
from joshua_shared.log import get_logger
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway import viewer_session
from joshua_gateway.viewer_api.edit import (
    _field,  # the one reader of a JSON write body, and of the origin of a write
    _fields,
    _origin_ok,
)

logger = get_logger("viewer")

# What a refused sign-in says. One message for a wrong secret, a person who is
# not on the roster, and a person with no secret, so the answer tells nobody
# which of the three it was.
SIGN_IN_FAILED = "That sign-in did not work."
# What a request says when the viewer cannot reach core: there is no sign-in
# without core, because core holds every secret.
NO_CORE = "Sign-in is not available."
# The longest secret core takes.
SECRET_MAX = 256

# The sections of the navigation: (key, label, URL).
SECTIONS = (
    ("wiki", "Wiki", "/wiki/"),
    ("journal", "Journal", "/journal"),
    ("search", "Search", "/search"),
    ("usage", "Usage", "/usage"),
    ("assignments", "Assignments", "/assignments"),
    ("documents", "Documents", "/documents"),
    ("chat", "Chat", "/chat"),
)
# The sections that a guest does not get.
MEMBER_SECTIONS = frozenset({"documents"})
# The sections whose URL ends with the id of the person of the request. The
# chat answers at ``/chat/<person id>``, so the item follows the identity of
# the person who reads the viewer.
PERSON_SECTIONS = frozenset({"chat"})


def sections(person: Person) -> list[dict[str, str]]:
    """The sections of a person: a guest gets no Documents.

    A section of ``PERSON_SECTIONS`` gets the id of the person on its URL.
    """
    member = is_member(person.role)
    out: list[dict[str, str]] = []
    for key, label, url in SECTIONS:
        if not member and key in MEMBER_SECTIONS:
            continue
        if key in PERSON_SECTIONS:
            url = f"{url}/{person.id}"
        out.append({"key": key, "label": label, "path": url})
    return out


def person_json(person: Person) -> dict[str, str]:
    """The person of the request, as the React app reads it."""
    return {"id": person.id, "name": person.name, "role": person.role}


async def session(request: Request, person: Person) -> JSONResponse:
    """The session of the person who signed in."""
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse(
        {
            "person": person_json(person),
            "anonymous": person.id == viewer.ANONYMOUS_PERSON_ID,
            "sections": sections(person),
            "chat_url": f"/chat/{person.id}",
            "timezone": config.load().timezone,
        },
        headers=viewer._NO_STORE,
    )


# -- the sign-in ------------------------------------------------------------


def _answer(body: dict[str, Any], status: int = 200) -> JSONResponse:
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse(body, status_code=status, headers=viewer._NO_STORE)


def _secret(fields: dict[str, Any], name: str) -> str:
    """One secret of a body, held to the length core takes. It is never
    logged, and it never reaches an answer."""
    return _field(fields, name)[:SECRET_MAX]


async def sign_in(request: Request) -> JSONResponse:
    """Check a person's secret with core, and answer with the cookie.

    A body that is not JSON, a cross-site origin, a wrong secret, a person who
    is not on the roster, and a person with no secret all get one refusal, so
    the answer says nothing about who exists. ``remember`` gives the cookie a
    month; without it the browser drops it when it closes.
    """
    fields = await _fields(request)
    if fields is None or not _origin_ok(request):
        return _answer({"error": SIGN_IN_FAILED}, 403)
    core = viewer_session.from_env(os.environ)
    if core is None:
        return _answer({"error": NO_CORE}, 503)
    person_id = _field(fields, "person").strip()
    secret = _secret(fields, "secret")
    remember = fields.get("remember") is True
    ok = bool(person_id) and bool(secret) and await core.verify(person_id, secret)
    person = config.load().person(person_id)
    if not ok or person is None:
        logger.info({"message": "viewer sign-in refused"})
        return _answer({"error": SIGN_IN_FAILED}, 401)
    stamp = await viewer_session.current_stamp(core, person_id)
    if stamp is None:
        return _answer({"error": NO_CORE}, 503)
    logger.info({"message": "viewer sign-in", "person": person_id, "remember": remember})
    response = _answer({"ok": True, "person": person_json(person)})
    viewer_session.set_cookie(
        response,
        viewer_session.issue(person_id, stamp, remember=remember),
        remember=remember,
        secure=viewer_session.is_secure(request),
    )
    return response


async def sign_out(request: Request) -> JSONResponse:
    """Clear the cookie.

    It needs no session: a cookie the viewer no longer accepts can still be
    thrown away, and the answer is the same either way.
    """
    if not _origin_ok(request):
        return _answer({"error": "forbidden"}, 403)
    response = _answer({"ok": True})
    viewer_session.clear_cookie(response, secure=viewer_session.is_secure(request))
    return response


async def change_secret(request: Request, person: Person) -> JSONResponse:
    """Change the secret of the person who signed in, through core.

    The current secret goes with the new one, so a wrong one is core's 403.
    The change moves the stamp of every outstanding cookie of that person, so
    the answer carries a new cookie and the person stays signed in here while
    every other browser signs out.
    """
    fields = await _fields(request)
    if fields is None or not _origin_ok(request):
        return _answer({"error": "forbidden"}, 403)
    core = viewer_session.from_env(os.environ)
    if core is None:
        return _answer({"error": NO_CORE}, 503)
    secret = _secret(fields, "secret")
    new_secret = _secret(fields, "new_secret")
    if not secret or not new_secret:
        return _answer({"error": "Both the current secret and the new one are needed."}, 400)
    status = await core.change_secret(person.id, secret, new_secret)
    if status != 200:
        logger.info({"message": "viewer secret refused", "status": status})
        return _answer({"error": "That change did not work."}, 403 if status == 403 else 503)
    viewer_session.forget(person.id)
    stamp = await viewer_session.current_stamp(core, person.id)
    if stamp is None:
        return _answer({"error": NO_CORE}, 503)
    logger.info({"message": "viewer secret changed", "person": person.id})
    session_cookie = request.cookies.get(viewer_session.COOKIE_NAME) or ""
    current = viewer_session.read(session_cookie)
    remember = current is not None and current.remember
    response = _answer({"ok": True})
    viewer_session.set_cookie(
        response,
        viewer_session.issue(person.id, stamp, remember=remember),
        remember=remember,
        secure=viewer_session.is_secure(request),
    )
    return response


async def auth_method(request: Request) -> JSONResponse:
    """How this instance asks a person to sign in, for the sign-in page.

    It needs no session, because the page reads it before there is one.
    ``anonymous`` says the viewer serves a request with no cookie, so the app
    shows no sign-in page whatever the method is.
    """
    cfg = config.load()
    return _answer(
        {
            "method": cfg.viewer.auth.method,
            "anonymous": cfg.viewer.anonymous is not False,
        }
    )


async def sign_in_people(request: Request) -> JSONResponse:
    """The people who can sign in with a pin, named as the roster names them.

    Core says who has a secret; the roster says the name and the role. A
    person core names who is not on the roster is left out. Any method but
    ``pin`` is 404: no other instance renders a roster to a stranger.
    """
    cfg = config.load()
    if cfg.viewer.auth.method != "pin":
        return _answer({"error": "not found"}, 404)
    core = viewer_session.from_env(os.environ)
    ids = await core.people() if core is not None else None
    if ids is None:
        return _answer({"error": NO_CORE}, 503)
    people = [cfg.person(person_id) for person_id in ids]
    return _answer({"people": [person_json(p) for p in people if p is not None]})
