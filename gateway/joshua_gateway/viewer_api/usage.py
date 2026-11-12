"""The JSON route of the usage page: the subscription limits, and the tokens.

``GET /api/v1/usage?days=<n>`` makes both calls to core at the same time:

- ``usage``: core's ``GET /v1/usage`` with the fleet token of the viewer
  (``viewer_usage``), as core sent it. ``usage_configured`` is false when
  ``CORE_URL`` or ``JOSHUA_TOKEN_VIEWER`` is missing, and ``usage`` is then
  null. A call that fails is null too.
- ``limits``: core's open ``GET /v1/limits`` (``viewer_limits``).
  ``limits_configured`` is false with no ``CORE_URL``, and ``limits`` is then
  null. A call that fails, a ``404`` (``limits.enabled`` is false), and an
  unknown shape each give null as well.

Every number of a meter is derived here, so the page shows it and computes
nothing: the percent used and left as text (``viewer_limits.pct``), the level
of the bar (``viewer_limits.level``), the fill of the bar held to 0 to 100,
the badge, and the reset time as a person reads it
(``viewer_limits.when_text``). ``now`` is the time in the instance timezone,
which the chart needs for the last day of its window.

The route answers 200 in every case, because the page says what is missing.
Nothing of core's answer is HTML here: the page escapes by construction.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, tzinfo
from typing import Any

from joshua_shared import config
from joshua_shared.config import Person
from joshua_shared.contracts import LimitsResponse, LimitsWindow
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway import viewer_limits as limits_mod
from joshua_gateway import viewer_usage as usage_mod

# The name of each window of the limits, in the order of the page.
WINDOWS = ("5-hour", "Weekly")

# What a meter says beside its name, by level. A window that core marks
# ``reset`` carries no badge, because its numbers are old.
_BADGES = {"crit": "critical", "warn": "high"}
_REJECTED = "rejected"
_REJECTED_BADGE = "blocked"


def _answer(body: dict[str, Any]) -> JSONResponse:
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse(body, headers=viewer._NO_STORE)


async def _none() -> None:
    return None


def _reset_text(window: LimitsWindow, *, tz: tzinfo, now: datetime) -> str:
    human = limits_mod.when_text(window.resets_at, tz=tz, now=now)
    if window.reset:
        at = f" at {human}" if human else ""
        return f"reset{at} — updates on the next model call"
    return f"resets {human}" if human else ""


def _window_json(name: str, window: LimitsWindow, *, tz: tzinfo, now: datetime) -> dict[str, Any]:
    """One meter, with every number already derived."""
    level = limits_mod.level(window.used_pct, window.status)
    if window.reset:
        badge = ""
    elif window.status == _REJECTED:
        badge = _REJECTED_BADGE
    else:
        badge = _BADGES.get(level, "")
    return {
        "name": name,
        "pct_used": limits_mod.pct(window.used_pct),
        "pct_left": limits_mod.pct(window.left_pct),
        "fill": max(0.0, min(window.used_pct, 100.0)),
        "level": level,
        "badge": badge,
        "reset_at": window.resets_at,
        "reset_text": _reset_text(window, tz=tz, now=now),
        "reset": window.reset,
    }


def limits_json(body: LimitsResponse | None, *, tz: tzinfo, now: datetime) -> dict[str, Any] | None:
    """The meters of core's answer, or None when core did not answer.

    An answer that is not available carries no window, and ``reason`` then
    says why core knows nothing yet.
    """
    if body is None:
        return None
    windows = []
    if body.available:
        pairs = zip(WINDOWS, (body.five_hour, body.seven_day), strict=True)
        windows = [
            _window_json(name, window, tz=tz, now=now)
            for name, window in pairs
            if window is not None
        ]
    return {
        "windows": windows,
        "as_of": limits_mod.when_text(body.observed_at, tz=tz, now=now),
        "reason": body.reason,
    }


async def usage(request: Request, person: Person) -> JSONResponse:
    """The subscription limits and the tokens of each group by day, from core.

    The two calls run at the same time. ``days`` is held to the window core
    allows (``viewer_usage.parse_days``), and a value that is not an integer
    gives the default.
    """
    from joshua_gateway import viewer  # ``viewer`` imports this package

    days = usage_mod.parse_days(request.query_params.get("days"))
    core = usage_mod.from_env(os.environ)
    limits_core = limits_mod.from_env(os.environ)
    body, limits = await asyncio.gather(
        core.fetch(days) if core is not None else _none(),
        limits_core.fetch() if limits_core is not None else _none(),
    )
    timezone = config.load().timezone
    tz = limits_mod.zone(timezone)
    now = viewer._now(tz)
    return _answer(
        {
            "days": days,
            "usage_configured": core is not None,
            "usage": body.model_dump() if body is not None else None,
            "limits_configured": limits_core is not None,
            "limits": limits_json(limits, tz=tz, now=now),
            "timezone": timezone,
            "now": now.isoformat(),
        }
    )
