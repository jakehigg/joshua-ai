"""The JSON routes of the workspaces: they proxy core's ``/v1/workspaces``.

- ``GET`` and ``POST /api/v1/workspaces`` list and create.
- ``GET /api/v1/workspaces/options`` gives the servers (a name and one line)
  and the models that the person may pick.
- ``GET`` and ``PATCH /api/v1/workspaces/{slug}`` read and change one.
- ``GET`` and ``POST /api/v1/workspaces/{slug}/items`` list and add items.
- ``PATCH`` and ``DELETE /api/v1/workspaces/{slug}/items/{id}`` change
  (``pinned``) or remove an item.

Each route calls core with ``CORE_URL`` and ``JOSHUA_TOKEN_VIEWER`` and names
the signed-in person in the ``person`` query parameter. A guest gets 403. Every
write checks the ``Origin`` header, as the chat turn does. Core's status and
JSON body go back as they are. With no ``CORE_URL`` or no token, the routes
answer 503, and a core that cannot be reached is 503 too.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from joshua_shared.config import Person, is_member
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway.viewer_api.edit import _origin_ok

logger = get_logger("viewer.workspaces")

CORE_URL_ENV = "CORE_URL"
VIEWER_TOKEN_ENV = "JOSHUA_TOKEN_VIEWER"
CORE_PATH = "/v1/workspaces"
TIMEOUT_S = 15.0
MAX_BODY_BYTES = 64 * 1024

FORBIDDEN = "workspaces are for members"
CROSS_SITE = "the request comes from another site"
NOT_CONFIGURED = "workspaces are not configured on the Joshua side"
UNREACHABLE = "Joshua is unreachable"
BAD_BODY = "the body must be a JSON object"


def _json(body: Any, status: int) -> JSONResponse:
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse(body, status_code=status, headers=viewer._NO_STORE)


def _error(message: str, status: int) -> JSONResponse:
    return _json({"error": message}, status)


async def _call(
    request: Request, person: Person, method: str, suffix: str, *, write: bool
) -> JSONResponse:
    """Check the person, the origin and the body, then call core."""
    if not is_member(person.role):
        return _error(FORBIDDEN, 403)
    if write and not _origin_ok(request):
        return _error(CROSS_SITE, 403)
    base, token = os.environ.get(CORE_URL_ENV), os.environ.get(VIEWER_TOKEN_ENV)
    if not base or not token:
        return _error(NOT_CONFIGURED, 503)
    body: Any = None
    if method in ("POST", "PATCH"):
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_BODY_BYTES:
                return _error("the body is too large", 413)
        try:
            body = await _parse(raw)
        except ValueError:
            return _error(BAD_BODY, 422)
    params = {"person": person.id}
    status_filter = request.query_params.get("status")
    if suffix == "" and method == "GET" and status_filter:
        params["status"] = status_filter
    try:
        async with FleetClient(base, token, TIMEOUT_S) as client:
            response = await client.request(method, CORE_PATH + suffix, params=params, json=body)
    except httpx.HTTPError as exc:
        logger.warning({"message": "workspaces: core unreachable", "error": type(exc).__name__})
        return _error(UNREACHABLE, 503)
    try:
        answer: Any = response.json()
    except ValueError:
        answer = {}
    if not isinstance(answer, dict):
        answer = {}
    return _json(answer, response.status_code)


async def _parse(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw) if raw.strip() else {}
    if not isinstance(value, dict):
        raise ValueError(BAD_BODY)
    return value


def _slug(request: Request) -> str:
    return request.path_params["slug"]


async def list_workspaces(request: Request, person: Person) -> JSONResponse:
    return await _call(request, person, "GET", "", write=False)


async def create(request: Request, person: Person) -> JSONResponse:
    return await _call(request, person, "POST", "", write=True)


async def options(request: Request, person: Person) -> JSONResponse:
    return await _call(request, person, "GET", "/options", write=False)


async def read(request: Request, person: Person) -> JSONResponse:
    return await _call(request, person, "GET", f"/{_slug(request)}", write=False)


async def change(request: Request, person: Person) -> JSONResponse:
    return await _call(request, person, "PATCH", f"/{_slug(request)}", write=True)


async def list_items(request: Request, person: Person) -> JSONResponse:
    return await _call(request, person, "GET", f"/{_slug(request)}/items", write=False)


async def add_item(request: Request, person: Person) -> JSONResponse:
    return await _call(request, person, "POST", f"/{_slug(request)}/items", write=True)


async def change_item(request: Request, person: Person) -> JSONResponse:
    path = f"/{_slug(request)}/items/{request.path_params['id']}"
    return await _call(request, person, "PATCH", path, write=True)


async def delete_item(request: Request, person: Person) -> JSONResponse:
    path = f"/{_slug(request)}/items/{request.path_params['id']}"
    return await _call(request, person, "DELETE", path, write=True)
