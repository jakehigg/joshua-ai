"""``GET /v1/usage`` and ``GET /admin/usage``: the tokens of each group by day.

The viewer calls ``/v1/usage`` for its usage page. An operator calls
``/admin/usage`` with an admin token, for example with ``curl``. Both routes
give the same answer: for each calendar day in the instance timezone, the
calls, the tokens, and the models of each group (``chat``, ``reflection``,
``internet``, and ``describe``). ``groups`` and
``totals`` always hold each feature of ``usage_features``, with zero counts
when it made no call. See ``store/usage_events.py``.

``?days=N`` is the window, today included: default 30, held to 1..366. A
value that is not an integer gets 422.

``usage.allowed_callers`` names the fleet identities that may call
``/v1/usage``. An empty list turns the route off, so every caller gets 403.
``/admin/usage`` checks ``ADMIN_CALLERS``, as every admin route does. The
answer holds counts, group names, and dates only: no person, no message, and
no question.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from joshua_shared.contracts import USAGE_DEFAULT_DAYS
from joshua_shared.fleet_auth import identify_bearer, load_fleet_tokens
from joshua_shared.log import get_logger

from joshua_core.admin import authorize_admin
from joshua_core.store.usage_events import clamp_days, summarize, usage_features, window_start

logger = get_logger("usage_api")

USAGE_PATH = "/v1/usage"
ADMIN_USAGE_PATH = "/admin/usage"

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _authorize(request: Request) -> JSONResponse | None:
    """A 401 or 403 answer, or None for a caller on ``usage.allowed_callers``.

    An empty ``allowed_callers`` denies every caller."""
    identity = identify_bearer(request.headers.get("authorization"), load_fleet_tokens())
    if identity is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    allowed = {
        name.strip().lower() for name in request.app.state.ctx.settings.usage.allowed_callers
    }
    if identity not in allowed:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


def _parse_days(raw: str | None) -> int | None:
    """The window in days, held to the bounds, or None for a value that is not
    an integer."""
    if raw is None or raw == "":
        return USAGE_DEFAULT_DAYS
    try:
        return clamp_days(int(raw))
    except ValueError:
        return None


async def _answer(request: Request, clock: Clock) -> JSONResponse:
    days = _parse_days(request.query_params.get("days"))
    if days is None:
        return JSONResponse(
            {"error": "invalid request", "detail": "days must be an integer"}, status_code=422
        )
    ctx = request.app.state.ctx
    timezone = ctx.settings.timezone
    since = window_start(clock(), timezone, days)
    rows = await ctx.repo.usage_rows(since, timezone)
    body = summarize(rows, timezone=timezone, days=days, features=usage_features(ctx.settings))
    return JSONResponse(body.model_dump())


def build_usage_router(clock: Clock | None = None) -> APIRouter:
    """The two usage routes. ``clock`` gives the time the window ends at."""
    now = clock or _utc_now
    router = APIRouter()

    @router.get(USAGE_PATH)
    async def usage(request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        return await _answer(request, now)

    @router.get(ADMIN_USAGE_PATH)
    async def admin_usage(request: Request):  # type: ignore[no-untyped-def]
        denied = authorize_admin(request)
        if denied is not None:
            return denied
        return await _answer(request, now)

    return router
