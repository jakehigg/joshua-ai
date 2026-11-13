"""The session cookie of the viewer, and the ``/auth/*`` calls it makes to core.

A person signs in on a form of the React app, the viewer asks core to check the
secret (``POST /auth/verify``), and the answer carries one signed cookie,
``joshua_session``. Every later request is that cookie: the viewer reads the
person out of it and makes no call to core for an ordinary page. Core owns the
roster and every hash, so the viewer holds no password of its own.

A cookie is ``<payload>.<signature>``. The payload is base64url JSON with the
person id, the expiry, whether "remember me" was checked, and the stamp of
that person's secret (the ``updated_at`` of their ``auth_secret`` row). The
signature is HMAC-SHA256 of the payload under
``JOSHUA_VIEWER_SESSION_SECRET``. With no such variable the process mints a
random secret at start, so every cookie dies with the process; set the
variable to keep a sign-in across a restart, and share it between replicas.

The stamp is what makes a changed secret sign a person out: the viewer reads
``GET /auth/people/<id>`` from core and refuses a cookie whose stamp is not
the one core holds. :data:`STAMP_TTL_S` seconds of cache hold core's answer,
so a page costs no call to core and a secret change still lands within the
minute. Core that answers 404 (the person has no secret) or cannot answer at
all refuses the cookie, and the person signs in again; a failure is never
cached. With no ``CORE_URL`` or ``JOSHUA_TOKEN_VIEWER`` there is no sign-in at
all, so a cookie this process signed stands on its signature and its expiry
alone.

The cookie is ``HttpOnly`` and ``SameSite=Lax``, on ``Path=/``, and
``Secure`` when the proxy in front says the request came over https
(``X-Forwarded-Proto``). "Remember me" gives it a 30-day ``Max-Age``; without
it the cookie carries none and the browser drops it when it closes, and the
signature expires a day later whatever the browser kept.

The routes are ``viewer_api.session``: the sign-in, the sign-out, the change
of a person's own secret, the method of the instance, and the list of the
people who can sign in with a pin.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger
from starlette.requests import Request
from starlette.responses import Response

logger = get_logger("viewer.session")

# The name of the one cookie of the viewer.
COOKIE_NAME = "joshua_session"

# The key that signs a cookie. With no value, ``_signing_key`` mints a random
# one for this process, and a restart signs everybody out.
SESSION_SECRET_ENV = "JOSHUA_VIEWER_SESSION_SECRET"

# How long a cookie lives: a month with "remember me", a day without, which is
# the life of the signature of a cookie the browser drops when it closes.
REMEMBER_MAX_AGE_S = 30 * 24 * 60 * 60
SESSION_MAX_AGE_S = 24 * 60 * 60

# How long the viewer holds core's answer about one person's secret. Short, so
# a changed secret signs out every outstanding cookie within the minute, and
# long enough that a page costs no call to core.
STAMP_TTL_S = 60

CORE_URL_ENV = "CORE_URL"
VIEWER_TOKEN_ENV = "JOSHUA_TOKEN_VIEWER"
VERIFY_PATH = "/auth/verify"
SECRET_PATH = "/auth/secret"
PEOPLE_PATH = "/auth/people"
PROFILE_PATH = "/people/{person}/profile"

# A person waits for a sign-in, so a slow core must not hold it open.
TIMEOUT_S = 5.0

# The header a reverse proxy sets when it terminated TLS.
_FORWARDED_PROTO = "X-Forwarded-Proto"

# The secret of this process, when the environment names none.
_PROCESS_KEY = secrets.token_bytes(32)

# person id -> (monotonic deadline, stamp). A failure is never stored.
_STAMPS: dict[str, tuple[float, str]] = {}


@dataclass(frozen=True)
class Session:
    """What one cookie says: the person, the expiry, and the stamp it carries."""

    person_id: str
    expires_at: int
    stamp: str
    remember: bool


def _signing_key() -> bytes:
    """The key that signs a cookie: the environment, or this process."""
    value = os.environ.get(SESSION_SECRET_ENV, "").strip()
    return value.encode("utf-8") if value else _PROCESS_KEY


def _now() -> int:
    """The time now, in whole seconds. A test replaces it to fix the clock."""
    return int(time.time())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(payload: str) -> str:
    return _b64(hmac.new(_signing_key(), payload.encode("ascii"), hashlib.sha256).digest())


def max_age(remember: bool) -> int | None:
    """The ``Max-Age`` of a cookie: a month, or None for the browser session."""
    return REMEMBER_MAX_AGE_S if remember else None


def issue(person_id: str, stamp: str, *, remember: bool = False) -> str:
    """One signed cookie value for a person who just proved who they are."""
    payload = _b64(
        json.dumps(
            {
                "p": person_id,
                "e": _now() + (REMEMBER_MAX_AGE_S if remember else SESSION_MAX_AGE_S),
                "s": stamp,
                "r": remember,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    return f"{payload}.{_signature(payload)}"


def read(token: str) -> Session | None:
    """The session of a cookie, or None when it is not ours or has expired.

    A cookie this key did not sign, one in another shape, and one past its
    expiry all fail the same way. This checks the signature and the clock
    only; :func:`person_from_cookie` also asks core about the secret.
    """
    payload, _, signature = token.partition(".")
    if not payload or not signature:
        return None
    if not hmac.compare_digest(_signature(payload), signature):
        return None
    try:
        body = json.loads(_unb64(payload))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    person_id, expires_at, stamp = body.get("p"), body.get("e"), body.get("s")
    if not isinstance(person_id, str) or not isinstance(expires_at, int):
        return None
    if not isinstance(stamp, str) or not person_id:
        return None
    if expires_at <= _now():
        return None
    return Session(
        person_id=person_id,
        expires_at=expires_at,
        stamp=stamp,
        remember=body.get("r") is True,
    )


def is_secure(request: Request) -> bool:
    """True when the request reached the proxy over https.

    The viewer serves plain HTTP behind a reverse proxy, so the proxy's
    ``X-Forwarded-Proto`` is what says whether the browser used TLS. With no
    such header the cookie carries no ``Secure``, because a plain-HTTP
    instance on a private network would never get the cookie back.
    """
    proto = request.headers.get(_FORWARDED_PROTO, "")
    return proto.split(",")[0].strip().lower() == "https"


def set_cookie(response: Response, token: str, *, remember: bool, secure: bool) -> None:
    """Put the session cookie on a response."""
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max_age(remember),
        path="/",
        httponly=True,
        samesite="lax",
        secure=secure,
    )


def clear_cookie(response: Response, *, secure: bool) -> None:
    """Take the session cookie off a response: the sign-out."""
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=secure)


# -- core ------------------------------------------------------------------


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """The JSON object of a core answer, or {} for any other body."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class CoreAuth:
    """The ``/auth/*`` conversation of the viewer with core."""

    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._transport = transport

    def _client(self) -> FleetClient:
        return FleetClient(self._base_url, self._token, TIMEOUT_S, transport=self._transport)

    async def verify(self, person_id: str, secret: str) -> bool:
        """True when core says the secret is that person's.

        Core answers ``ok: false`` for a wrong secret, for a person with no
        secret, and for one who is not on the roster, and never says which.
        Core that cannot answer is false too: no sign-in without core.
        """
        try:
            async with self._client() as client:
                response = await client.post_json(
                    VERIFY_PATH, {"person": person_id, "secret": secret}
                )
        except httpx.HTTPError as exc:
            logger.warning({"message": "core verify failed", "error": type(exc).__name__})
            return False
        if response.status_code != 200:
            logger.warning({"message": "core verify refused", "status": response.status_code})
            return False
        try:
            return response.json().get("ok") is True
        except ValueError:
            logger.warning({"message": "core verify answered an unknown shape"})
            return False

    async def change_secret(self, person_id: str, secret: str, new_secret: str) -> int:
        """Change a person's own secret through core, and return core's status.

        The current secret goes with the new one, so a wrong one is core's
        403. A core that cannot answer is 503, as for any other outage.
        """
        try:
            async with self._client() as client:
                response = await client.post_json(
                    SECRET_PATH,
                    {"person": person_id, "secret": secret, "new_secret": new_secret},
                )
        except httpx.HTTPError as exc:
            logger.warning({"message": "core secret failed", "error": type(exc).__name__})
            return 503
        return response.status_code

    async def profile(self, person_id: str) -> tuple[int, dict[str, Any]]:
        """The ``prompt`` and ``journal`` of a person, as core gives them.

        Returns core's status and its JSON body. A core that cannot answer is
        503 with an empty body.
        """
        try:
            async with self._client() as client:
                response = await client.get_json(PROFILE_PATH.format(person=person_id))
        except httpx.HTTPError as exc:
            logger.warning({"message": "core profile failed", "error": type(exc).__name__})
            return 503, {}
        return response.status_code, _json_object(response)

    async def set_profile(
        self, person_id: str, fields: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """Write ``prompt`` and, or, ``journal`` of a person through core.

        Returns core's status and its JSON body, which carries the new profile
        or an error. A core that cannot answer is 503 with an empty body.
        """
        try:
            async with self._client() as client:
                response = await client.post_json(PROFILE_PATH.format(person=person_id), fields)
        except httpx.HTTPError as exc:
            logger.warning({"message": "core profile write failed", "error": type(exc).__name__})
            return 503, {}
        return response.status_code, _json_object(response)

    async def people(self) -> list[str] | None:
        """The ids of the people who can sign in, or None when core did not
        answer. Core gives no time here, only the ids."""
        try:
            async with self._client() as client:
                response = await client.get_json(PEOPLE_PATH)
        except httpx.HTTPError as exc:
            logger.warning({"message": "core people failed", "error": type(exc).__name__})
            return None
        if response.status_code != 200:
            logger.warning({"message": "core people refused", "status": response.status_code})
            return None
        try:
            people = response.json().get("people")
        except ValueError:
            people = None
        if not isinstance(people, list):
            logger.warning({"message": "core people answered an unknown shape"})
            return None
        return [person for person in people if isinstance(person, str)]

    async def stamp(self, person_id: str) -> str | None:
        """When that person's secret last changed, as core writes it, or None.

        None is "no usable secret, or core could not say": a 404 for a person
        with no secret, any other status, and an outage all give it, and the
        caller refuses the cookie.
        """
        try:
            async with self._client() as client:
                response = await client.get_json(f"{PEOPLE_PATH}/{person_id}")
        except httpx.HTTPError as exc:
            logger.warning({"message": "core stamp failed", "error": type(exc).__name__})
            return None
        if response.status_code != 200:
            return None
        try:
            updated_at = response.json().get("updated_at")
        except ValueError:
            updated_at = None
        if not isinstance(updated_at, str) or not updated_at:
            logger.warning({"message": "core stamp answered an unknown shape"})
            return None
        return updated_at


def from_env(env: Mapping[str, str]) -> CoreAuth | None:
    """A client from ``CORE_URL`` and ``JOSHUA_TOKEN_VIEWER``, or None.

    None means the viewer cannot reach core: there is no sign-in, and no
    stamp to check a cookie against.
    """
    base_url = env.get(CORE_URL_ENV)
    token = env.get(VIEWER_TOKEN_ENV)
    if not base_url or not token:
        return None
    return CoreAuth(base_url, token)


# -- the stamp of a person, cached -----------------------------------------


def forget(person_id: str) -> None:
    """Drop the cached stamp of one person, after their secret changed here."""
    _STAMPS.pop(person_id, None)


async def current_stamp(core: CoreAuth, person_id: str) -> str | None:
    """The stamp of a person's secret, from the cache or from core.

    The cache holds an answer for :data:`STAMP_TTL_S` seconds and never holds
    a failure, so a core outage costs a call each request and a secret change
    lands within the minute.
    """
    cached = _STAMPS.get(person_id)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    stamp = await core.stamp(person_id)
    if stamp is None:
        _STAMPS.pop(person_id, None)
        return None
    _STAMPS[person_id] = (time.monotonic() + STAMP_TTL_S, stamp)
    return stamp


async def person_from_cookie(request: Request) -> str | None:
    """The id of the person a request's cookie names, or None.

    None is every failure: no cookie, a cookie this key did not sign, an
    expired one, one whose stamp is not the one core holds, and one core
    cannot vouch for. The caller answers 401 and the app shows the sign-in.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    session = read(token)
    if session is None:
        return None
    core = from_env(os.environ)
    if core is None:
        # Nothing to check against, and nothing could have issued this cookie
        # but this process: the signature and the expiry are the whole check.
        return session.person_id
    stamp = await current_stamp(core, session.person_id)
    if stamp is None or not hmac.compare_digest(stamp, session.stamp):
        logger.info({"message": "viewer session refused", "reason": "stamp"})
        return None
    return session.person_id
