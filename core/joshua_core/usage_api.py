"""``GET /v1/usage`` and ``GET /admin/usage``: the tokens of each group by day.
``POST /v1/usage``: a usage row that another container reports.

The viewer calls ``/v1/usage`` for its usage page. An operator calls
``/admin/usage`` with an admin token, for example with ``curl``. Both routes
give the same answer: for each calendar day in the instance timezone, the
calls, the tokens, and the models of each group (``chat``, ``reflection``,
``internet``, ``describe``, and ``assignment``). ``groups`` and
``totals`` always hold each feature of ``usage_features``, with zero counts
when it made no call. See ``store/usage_events.py``.

``?days=N`` is the window, today included: default 30, held to 1..366. A
value that is not an integer gets 422.

``usage.allowed_callers`` names the fleet identities that may call
``/v1/usage``. An empty list turns the route off, so every caller gets 403.
``/admin/usage`` checks ``ADMIN_CALLERS``, as every admin route does. The
answer holds counts, group names, and dates only: no person, no message, and
no question.

``POST /v1/usage`` writes usage rows that a container reports for its own
model calls. ``usage.writers`` names the fleet identities that may call it
(default ``assignments``). An empty list turns it off, so every caller gets
403. The body is a ``UsageWriteRequest``: at most ``USAGE_WRITE_MAX_ROWS``
rows and ``USAGE_WRITE_MAX_BYTES`` bytes (413). Each row must be of kind
``assignment``. A row of a kind that core writes itself, or any other body
that is not valid, gets 400 and core writes no row. Core stores the ``ref``
of a row as ``<writer>:<ref>``, so a writer cannot touch the rows of core or
of another writer, and a row sent again is not written again. The route logs
the writer and the number of rows, never a row.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from joshua_shared.contracts import (
    USAGE_DEFAULT_DAYS,
    USAGE_WRITE_MAX_BYTES,
    USAGE_WRITE_PATH,
    UsageWriteRequest,
    UsageWriteResponse,
    UsageWriteRow,
)
from joshua_shared.fleet_auth import identify_bearer, load_fleet_tokens
from joshua_shared.log import get_logger
from pydantic import ValidationError

from joshua_core.admin import authorize_admin
from joshua_core.engine.usage import Usage
from joshua_core.store.usage_events import (
    UsageEvent,
    clamp_days,
    summarize,
    usage_features,
    window_start,
)

logger = get_logger("usage_api")

USAGE_PATH = "/v1/usage"
ADMIN_USAGE_PATH = "/admin/usage"

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _identify(request: Request, callers: list[str]) -> tuple[JSONResponse | None, str | None]:
    """``(denial, None)`` with a 401 or 403 answer, or ``(None, identity)``
    for a caller in ``callers``. An empty ``callers`` denies every caller."""
    identity = identify_bearer(request.headers.get("authorization"), load_fleet_tokens())
    if identity is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401), None
    if identity not in {name.strip().lower() for name in callers}:
        return JSONResponse({"error": "forbidden"}, status_code=403), None
    return None, identity


def _authorize(request: Request) -> JSONResponse | None:
    """A 401 or 403 answer, or None for a caller on ``usage.allowed_callers``."""
    denied, _ = _identify(request, request.app.state.ctx.settings.usage.allowed_callers)
    return denied


async def _read_body(request: Request) -> bytes | None:
    """The body, or None when it is over ``USAGE_WRITE_MAX_BYTES``. The size is
    checked as the body arrives."""
    length = request.headers.get("content-length")
    if length is not None and (not length.isdigit() or int(length) > USAGE_WRITE_MAX_BYTES):
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > USAGE_WRITE_MAX_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _event(writer: str, row: UsageWriteRow) -> UsageEvent:
    """The `usage_event` row of one reported row. The ref holds the writer."""
    counts = row.usage
    return UsageEvent(
        kind=row.kind,
        name=row.name,
        usage=Usage(
            input=counts.input,
            output=counts.output,
            cache_read=counts.cache_read,
            cache_write=counts.cache_write,
        ),
        calls=row.calls,
        worker=row.worker,
        model=row.model,
        person_id=row.person_id,
        run_id=row.run_id,
        endpoint=row.endpoint,
        ref=f"{writer}:{row.ref}",
    )


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
    """The three usage routes. ``clock`` gives the time the window ends at."""
    now = clock or _utc_now
    router = APIRouter()

    @router.get(USAGE_PATH)
    async def usage(request: Request):  # type: ignore[no-untyped-def]
        denied = _authorize(request)
        if denied is not None:
            return denied
        return await _answer(request, now)

    @router.post(USAGE_WRITE_PATH)
    async def write_usage(request: Request):  # type: ignore[no-untyped-def]
        # The bearer is checked before the body is read.
        denied, writer = _identify(request, request.app.state.ctx.settings.usage.writers)
        if denied is not None:
            return denied
        assert writer is not None
        raw = await _read_body(request)
        if raw is None:
            logger.warning({"message": "usage write refused: body too large", "writer": writer})
            return JSONResponse({"error": "body too large"}, status_code=413)
        try:
            body = UsageWriteRequest.model_validate_json(raw)
        except ValidationError as exc:
            detail = exc.errors(include_url=False, include_input=False, include_context=False)
            logger.warning({"message": "usage write refused: invalid body", "writer": writer})
            return JSONResponse({"error": "invalid request", "detail": detail}, status_code=400)
        repo = request.app.state.ctx.repo
        try:
            for row in body.rows:
                await repo.add_usage_event(_event(writer, row))
        except Exception as exc:  # noqa: BLE001 — the writer sends the rows again
            logger.warning(
                {"message": "usage write failed", "writer": writer, "error": type(exc).__name__}
            )
            return JSONResponse({"error": "usage not written"}, status_code=503)
        logger.info({"message": "usage written", "writer": writer, "rows": len(body.rows)})
        return JSONResponse(UsageWriteResponse(accepted=len(body.rows)).model_dump())

    @router.get(ADMIN_USAGE_PATH)
    async def admin_usage(request: Request):  # type: ignore[no-untyped-def]
        denied = authorize_admin(request)
        if denied is not None:
            return denied
        return await _answer(request, now)

    return router
