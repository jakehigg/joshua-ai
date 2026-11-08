"""The windows of a ``RateLimitEvent``: the 5-hour window and the weekly window.

Each model call on the Claude subscription sends a ``RateLimitEvent``. Its
``rate_limit_info`` says how much of each window is used. Core reads it to
keep its snapshot of the limits (``joshua_core.limits``). The assignments
container reads it during a slice, to stop at the ceiling of an assignment.

``parse_event_windows`` reads the event with care: a missing or unknown field
never fails, and gives no window.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# The two windows, as the event and core's ``GET /v1/limits`` name them.
WINDOWS = ("five_hour", "seven_day")


@dataclass(frozen=True)
class Window:
    """One window: the percent used (0 to 100), the reset time, and the status word."""

    used_pct: float
    resets_at: datetime | None = None
    status: str | None = None


def number(value: Any) -> float | None:
    """A finite number, or None. A bool is not a number here."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def text(value: Any) -> str | None:
    """A string with text, stripped, or None."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def epoch_time(value: Any) -> datetime | None:
    """Epoch seconds as a UTC time, or None."""
    seconds = number(value)
    if seconds is None or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def pct(value: float) -> float:
    """A percent, rounded to one decimal place."""
    return round(value, 1)


def parse_event_windows(info: Any) -> dict[str, Window]:
    """The windows of the ``rate_limit_info`` of a ``RateLimitEvent``.

    It reads ``raw["unifiedWindows"]`` first: a fraction of 0 to 1 and epoch
    seconds. With none, it reads ``rate_limit_type`` with ``utilization``.
    """
    raw = getattr(info, "raw", None)
    windows: dict[str, Window] = {}
    unified = raw.get("unifiedWindows") if isinstance(raw, Mapping) else None
    if isinstance(unified, Mapping):
        for key in WINDOWS:
            item = unified.get(key)
            if not isinstance(item, Mapping):
                continue
            used = number(item.get("utilization"))
            if used is None:
                continue
            windows[key] = Window(
                pct(used * 100), epoch_time(item.get("resetsAt")), text(item.get("status"))
            )
        if windows:
            return windows
    kind = getattr(info, "rate_limit_type", None)
    used = number(getattr(info, "utilization", None))
    if kind in WINDOWS and used is not None:
        windows[kind] = Window(
            pct(used * 100),
            epoch_time(getattr(info, "resets_at", None)),
            text(getattr(info, "status", None)),
        )
    return windows
