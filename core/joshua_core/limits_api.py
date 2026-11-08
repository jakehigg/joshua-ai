"""``GET /v1/limits``: how much of the Claude subscription limits is used.

The route has no bearer, as ``/healthz``. Core is not exposed outside the
cluster, or outside the host on compose (``127.0.0.1:8081``), and the answer
holds percentages, reset times, and the extra usage amounts only: no person,
no message, and no token. ``limits.enabled: false`` makes it answer 404.

The answer comes from ``joshua_core.limits``. A request reads the usage
endpoint only when the last good read is older than ``limits.max_age`` and no
block is in force. No parameter can force a read.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from joshua_shared.contracts import LIMITS_PATH


def build_limits_router() -> APIRouter:
    router = APIRouter()

    @router.get(LIMITS_PATH)
    async def limits(request: Request):  # type: ignore[no-untyped-def]
        ctx = getattr(request.app.state, "ctx", None)
        tracker = getattr(ctx, "limits", None)
        if ctx is None or not ctx.settings.limits.enabled or tracker is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        answer = await tracker.current()
        return JSONResponse(answer.model_dump())

    return router
