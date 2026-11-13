"""The JSON routes of the Users section: the roster, as the owner manages it.

Core owns the roster and every secret, so each route here is a call to core
with ``CORE_URL`` and ``JOSHUA_TOKEN_VIEWER``. The viewer calls core's
``/admin/people`` routes with its own fleet token, as the identity ``viewer``.
Core admits it there because ``viewer`` is in core's ``AUTH_CALLERS``, the
callers of ``/auth/*``.

- ``GET /api/v1/admin/users`` — the roster of ``joshua.yaml``, with
  ``has_secret`` for each person from core's ``GET /auth/people``.
- ``POST /api/v1/admin/users`` — ``{id, name, handle?, role?, secret?}``: core's
  ``POST /admin/people`` adds the person, then ``POST /auth/secret/admin``
  gives them the secret they sign in with. ``secret_set`` says whether the
  second call happened and worked; a person added with no secret cannot sign
  in to the viewer, and the owner can set one later.
- ``POST /api/v1/admin/users/{id}/secret`` — ``{secret}``: set the secret of a
  person without their current one, for a first sign-in or a reset
  (``POST /auth/secret/admin``).
- ``POST /api/v1/admin/users/{id}/role`` — ``{role}``: core's
  ``POST /admin/people/{id}/role``.
- ``POST /api/v1/admin/users/{id}/remove`` — core's
  ``POST /admin/people/{id}/remove``.

Core refuses, with 409, a role change or a remove that leaves no owner.

Every route is the owner's: a person whose role is not ``owner`` gets 403, so
a member and a guest alike are refused before any call to core. A member role
is not enough here, because this is the roster. Every write checks the
``Origin`` header, as the other writes of the viewer do.

Core's status and JSON body go back as they are. With no ``CORE_URL`` or no
token the routes answer 503, and a core that cannot be reached is 503 too. No
answer and no log line carries a secret.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from joshua_shared import config
from joshua_shared.config import Person
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway.viewer_api.edit import _field, _fields, _origin_ok
from joshua_gateway.viewer_api.session import SECRET_MAX

logger = get_logger("viewer.admin")

CORE_URL_ENV = "CORE_URL"
VIEWER_TOKEN_ENV = "JOSHUA_TOKEN_VIEWER"
PEOPLE_PATH = "/admin/people"
AUTH_PEOPLE_PATH = "/auth/people"
SECRET_PATH = "/auth/secret/admin"
TIMEOUT_S = 15.0

# The one role that manages the roster.
OWNER_ROLE = "owner"
# The role of a person the owner adds without naming one.
DEFAULT_ROLE = "member"

FORBIDDEN = "the roster is the owner's"
CROSS_SITE = "the request comes from another site"
NOT_CONFIGURED = "the roster is not configured on the Joshua side"
UNREACHABLE = "Joshua is unreachable"
BAD_BODY = "the body must be a JSON object"
NO_SECRET = "a secret is needed"
SECRET_FAILED = "that person was added, and their secret was not set"


def _json(body: Any, status: int = 200) -> JSONResponse:
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse(body, status_code=status, headers=viewer._NO_STORE)


def _error(message: str, status: int) -> JSONResponse:
    return _json({"error": message}, status)


def _guard(request: Request, person: Person, *, write: bool) -> JSONResponse | None:
    """The refusal of a request that is not the owner's, else None."""
    if person.role != OWNER_ROLE:
        return _error(FORBIDDEN, 403)
    if write and not _origin_ok(request):
        return _error(CROSS_SITE, 403)
    return None


async def _call(method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
    """One call to core: its status and its JSON object.

    A missing ``CORE_URL`` or token, a core that cannot be reached, and a body
    that is not a JSON object all give 503 with an ``error``, because the
    owner can do nothing here without core.
    """
    base, token = os.environ.get(CORE_URL_ENV), os.environ.get(VIEWER_TOKEN_ENV)
    if not base or not token:
        return 503, {"error": NOT_CONFIGURED}
    try:
        async with FleetClient(base, token, TIMEOUT_S) as client:
            response = await client.request(method, path, json=body)
    except httpx.HTTPError as exc:
        logger.warning({"message": "admin users: core unreachable", "error": type(exc).__name__})
        return 503, {"error": UNREACHABLE}
    try:
        answer: Any = response.json()
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        return 503, {"error": UNREACHABLE}
    return response.status_code, answer


def _target(request: Request) -> str:
    """The id of the person a route names in its path."""
    return request.path_params["id"]


async def users(request: Request, person: Person) -> JSONResponse:
    """The roster, and who of it can sign in.

    The names and the roles come from ``joshua.yaml``, which the viewer reads
    itself; core says which of those people hold a secret. An answer from core
    that does not hold the list of people is 503, so the page never says
    "nobody can sign in" because core said nothing.
    """
    denied = _guard(request, person, write=False)
    if denied is not None:
        return denied
    status, answer = await _call("GET", AUTH_PEOPLE_PATH)
    if status != 200:
        return _json(answer, status)
    people = answer.get("people")
    if not isinstance(people, list):
        logger.warning({"message": "admin users: core answered an unknown shape"})
        return _error(UNREACHABLE, 503)
    with_secret = {person_id for person_id in people if isinstance(person_id, str)}
    return _json(
        {
            "users": [
                {
                    "id": each.id,
                    "name": each.name,
                    "role": each.role,
                    "has_secret": each.id in with_secret,
                }
                for each in config.load().people
            ]
        }
    )


async def add(request: Request, person: Person) -> JSONResponse:
    """Add a person to the roster, and give them their secret.

    The two calls are core's: ``POST /admin/people`` and then
    ``POST /auth/secret/admin``. A refusal of the first one passes through and
    no secret is set. A refusal of the second one passes through too, with the
    person core already added, so the owner can set the secret again.
    """
    denied = _guard(request, person, write=True)
    if denied is not None:
        return denied
    fields = await _fields(request)
    if fields is None:
        return _error(BAD_BODY, 422)
    person_id = _field(fields, "id").strip()
    secret = _field(fields, "secret")[:SECRET_MAX]
    status, answer = await _call(
        "POST",
        PEOPLE_PATH,
        {
            "id": person_id,
            "name": _field(fields, "name").strip(),
            "handle": _field(fields, "handle").strip(),
            "role": _field(fields, "role").strip() or DEFAULT_ROLE,
        },
    )
    if status != 201:
        return _json(answer, status)
    logger.info({"message": "admin users: person added", "person": person_id})
    if not secret:
        return _json({**answer, "secret_set": False}, status)
    secret_status, secret_answer = await _call(
        "POST", SECRET_PATH, {"person": person_id, "new_secret": secret}
    )
    if secret_status != 200:
        logger.warning(
            {
                "message": "admin users: the secret of a new person was refused",
                "person": person_id,
                "status": secret_status,
            }
        )
        return _json({**answer, "secret_set": False, "error": SECRET_FAILED}, secret_status)
    return _json({**answer, "secret_set": True}, status)


async def set_secret(request: Request, person: Person) -> JSONResponse:
    """Set the secret of one person, without their current one.

    This is the first sign-in of a person the owner just added, and the reset
    of a person who lost theirs (core's ``POST /auth/secret/admin``).
    """
    denied = _guard(request, person, write=True)
    if denied is not None:
        return denied
    fields = await _fields(request)
    if fields is None:
        return _error(BAD_BODY, 422)
    secret = _field(fields, "secret")[:SECRET_MAX]
    if not secret:
        return _error(NO_SECRET, 400)
    target = _target(request)
    status, answer = await _call("POST", SECRET_PATH, {"person": target, "new_secret": secret})
    logger.info({"message": "admin users: secret set", "person": target, "status": status})
    return _json(answer, status)


async def set_role(request: Request, person: Person) -> JSONResponse:
    """Change the role of one person. Only the role changes."""
    denied = _guard(request, person, write=True)
    if denied is not None:
        return denied
    fields = await _fields(request)
    if fields is None:
        return _error(BAD_BODY, 422)
    target = _target(request)
    role = _field(fields, "role").strip()
    status, answer = await _call("POST", f"{PEOPLE_PATH}/{target}/role", {"role": role})
    logger.info(
        {"message": "admin users: role set", "person": target, "role": role, "status": status}
    )
    return _json(answer, status)


async def remove(request: Request, person: Person) -> JSONResponse:
    """Take one person off the roster: core drops their handles and secret.

    The route takes no body: the person is the one the path names.
    """
    denied = _guard(request, person, write=True)
    if denied is not None:
        return denied
    target = _target(request)
    status, answer = await _call("POST", f"{PEOPLE_PATH}/{target}/remove")
    logger.info({"message": "admin users: person removed", "person": target, "status": status})
    return _json(answer, status)
