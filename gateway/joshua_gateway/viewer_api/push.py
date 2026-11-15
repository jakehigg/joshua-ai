"""The JSON routes of web push: they proxy core's ``/v1/push``.

- ``GET /api/v1/push/key`` gives the public VAPID key the browser subscribes
  with.
- ``POST /api/v1/push/subscribe`` stores the subscription of this browser, or
  updates the one it already has. The body is what
  ``PushSubscription.toJSON()`` gives: ``{endpoint, keys: {p256dh, auth}}``.
  The viewer adds the ``User-Agent`` of the request, so the owner can tell two
  devices apart.
- ``DELETE /api/v1/push/subscribe`` — ``{endpoint}``: drops one subscription.
- ``POST /api/v1/push/test`` sends the signed-in person a test notification.

Every route is the signed-in person's alone: the person goes to core in the
``person`` query parameter and never in a body, so one person can neither read
nor remove the subscription of another. Every write checks the ``Origin``
header, as the workspace writes do.

Core holds the subscriptions and the VAPID keys, and core sends: the private
key is a secret setting that never leaves it. While push is off, a key is
missing, or ``JOSHUA_SETTINGS_KEY`` is unset, core answers 503 and its sentence
passes through, so the page can say what the owner has to do. With no
``CORE_URL`` or no token the routes answer 503 too, and so does a core that
cannot be reached.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from joshua_shared.config import Person
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway.viewer_api.edit import _origin_ok

logger = get_logger("viewer.push")

CORE_URL_ENV = "CORE_URL"
VIEWER_TOKEN_ENV = "JOSHUA_TOKEN_VIEWER"
CORE_PATH = "/v1/push"
TIMEOUT_S = 15.0
# A subscription is one endpoint and two short keys, and nothing else.
MAX_BODY_BYTES = 8 * 1024
# As long as the column of the subscription holds.
USER_AGENT_MAX = 300

CROSS_SITE = "the request comes from another site"
NOT_CONFIGURED = "notifications are not configured on the Joshua side"
UNREACHABLE = "Joshua is unreachable"
BAD_BODY = "the body must be a JSON object"


def _json(body: Any, status: int) -> JSONResponse:
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse(body, status_code=status, headers=viewer._NO_STORE)


def _error(message: str, status: int) -> JSONResponse:
    return _json({"error": message}, status)


async def _body(request: Request) -> dict[str, Any] | None:
    """The JSON object of a write, or None when it is not one."""
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY_BYTES:
            return None
    try:
        value = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


async def _call(
    request: Request, person: Person, method: str, suffix: str, *, body: Any = None
) -> JSONResponse:
    """One call to core, for the signed-in person."""
    base, token = os.environ.get(CORE_URL_ENV), os.environ.get(VIEWER_TOKEN_ENV)
    if not base or not token:
        return _error(NOT_CONFIGURED, 503)
    try:
        async with FleetClient(base, token, TIMEOUT_S) as client:
            response = await client.request(
                method, CORE_PATH + suffix, params={"person": person.id}, json=body
            )
    except httpx.HTTPError as exc:
        logger.warning({"message": "push: core unreachable", "error": type(exc).__name__})
        return _error(UNREACHABLE, 503)
    try:
        answer: Any = response.json()
    except ValueError:
        answer = {}
    if not isinstance(answer, dict):
        answer = {}
    return _json(answer, response.status_code)


async def key(request: Request, person: Person) -> JSONResponse:
    """The public VAPID key, or core's 503 while push is not configured."""
    return await _call(request, person, "GET", "/key")


async def subscribe(request: Request, person: Person) -> JSONResponse:
    """Store the subscription this browser just made."""
    if not _origin_ok(request):
        return _error(CROSS_SITE, 403)
    body = await _body(request)
    if body is None:
        return _error(BAD_BODY, 422)
    agent = request.headers.get("user-agent", "")[:USER_AGENT_MAX]
    # Only the three fields core takes: ``PushSubscription.toJSON()`` also
    # holds ``expirationTime``, which nothing here uses.
    sent = {
        "endpoint": body.get("endpoint"),
        "keys": body.get("keys"),
        "user_agent": agent,
    }
    return await _call(request, person, "POST", "/subscribe", body=sent)


async def unsubscribe(request: Request, person: Person) -> JSONResponse:
    """Drop one subscription of this person, by endpoint."""
    if not _origin_ok(request):
        return _error(CROSS_SITE, 403)
    body = await _body(request)
    if body is None:
        return _error(BAD_BODY, 422)
    return await _call(
        request, person, "DELETE", "/subscribe", body={"endpoint": body.get("endpoint")}
    )


async def test(request: Request, person: Person) -> JSONResponse:
    """Send this person a test notification now."""
    if not _origin_ok(request):
        return _error(CROSS_SITE, 403)
    return await _call(request, person, "POST", "/test", body={})
