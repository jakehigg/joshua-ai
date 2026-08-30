"""Channels admin routes for the inbound guard.

Every ``/admin/*`` route requires an identity in ``ADMIN_CALLERS`` (env, default
``laptop,ci``) — checked on every route with no exception. A missing or invalid
bearer is 401; a valid identity outside the allowlist is 403.

Routes:

- ``GET /admin/guard/stats`` — the refusal and truncation counters.
- ``GET /admin/guard/recent`` — the recent refusals; ``address`` is the handle an
  operator enrolls.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from joshua_shared.fleet_auth import require_identity

from joshua_channels.guard import REASON_TRUNCATED, REFUSAL_REASONS


def _empty_stats() -> dict[str, int]:
    counts = {reason: 0 for reason in REFUSAL_REASONS}
    counts[REASON_TRUNCATED] = 0
    counts["refused_total"] = 0
    return counts


def build_admin_router() -> APIRouter:
    """The guard admin routes. Both are gated by ``ADMIN_CALLERS``."""
    router = APIRouter()

    @router.get("/admin/guard/stats")
    async def guard_stats(  # type: ignore[no-untyped-def]
        request: Request,
        _identity: str = Depends(require_identity(admin=True)),
    ):
        guard = getattr(request.app.state.ctx, "guard", None)
        return JSONResponse(guard.stats() if guard is not None else _empty_stats())

    @router.get("/admin/guard/recent")
    async def guard_recent(  # type: ignore[no-untyped-def]
        request: Request,
        _identity: str = Depends(require_identity(admin=True)),
    ):
        guard = getattr(request.app.state.ctx, "guard", None)
        return JSONResponse({"recent": guard.recent() if guard is not None else []})

    return router
