"""The subscription limits on the usage page: one call to core, and its HTML.

The viewer asks core's ``GET /v1/limits`` and shows one meter for the 5-hour
window and one for the weekly window. A meter shows the percent used, a bar,
the percent left, and the reset time in the instance timezone. A line under
the heading says when the numbers came: the last model call on the
subscription. A window that core marks ``reset`` keeps its last bar, dimmed,
and says that the next model call updates it.

The route is open, so the viewer sends no bearer and needs only ``CORE_URL``.
With no ``CORE_URL``, :func:`from_env` returns None. A call that fails, a
``404`` (``limits.enabled`` is false), and an unknown shape each give None,
and the section then says that the limits are not available. The rest of the
usage page does not change.

The bar is inline SVG with no script, and its color comes from a class in the
viewer's stylesheet. Every name, reason, and status from core is escaped.

The functions here build data and HTML only. The route lives in ``viewer``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, tzinfo
from html import escape
from zoneinfo import ZoneInfo

import httpx
from joshua_shared.contracts import LIMITS_PATH, LimitsResponse, LimitsWindow
from joshua_shared.log import get_logger
from pydantic import ValidationError

logger = get_logger("viewer.limits")

CORE_URL_ENV = "CORE_URL"

# A person waits for this page, so a slow core must not hold it open.
TIMEOUT_S = 5.0

# The percent used where a meter starts to show a warning, and where it is
# critical.
WARN_PCT = 75.0
CRIT_PCT = 90.0


class CoreLimits:
    """Read the subscription limits from core. The route has no bearer."""

    def __init__(self, base_url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base_url = base_url
        self._transport = transport

    async def fetch(self) -> LimitsResponse | None:
        """Return core's answer, or None when core cannot answer."""
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=TIMEOUT_S, transport=self._transport
            ) as client:
                response = await client.get(LIMITS_PATH)
        except httpx.HTTPError as exc:
            logger.warning({"message": "core limits failed", "error": type(exc).__name__})
            return None
        if response.status_code != 200:
            logger.warning({"message": "core limits refused", "status": response.status_code})
            return None
        try:
            # A newer core can add a field, so an unknown field is ignored.
            return LimitsResponse.model_validate(response.json(), extra="ignore")
        except (ValueError, ValidationError):
            logger.warning({"message": "core limits answered an unknown shape"})
            return None


def from_env(env: Mapping[str, str]) -> CoreLimits | None:
    """A client from ``CORE_URL``, or None."""
    base_url = env.get(CORE_URL_ENV)
    return CoreLimits(base_url) if base_url else None


def zone(name: str) -> tzinfo:
    """The zone of ``name``; UTC when the name is not a zone."""
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError):
        return UTC


def level(used_pct: float, status: str | None = None) -> str:
    """The class of a meter: ``ok``, ``warn``, or ``crit``."""
    if status == "rejected" or used_pct >= CRIT_PCT:
        return "crit"
    if used_pct >= WARN_PCT:
        return "warn"
    return "ok"


def pct(value: float) -> str:
    """A percent with no decimal when it is whole: ``61%``, ``61.5%``."""
    text = f"{value:.1f}".removesuffix(".0")
    return f"{text}%"


def clock_time(when: datetime) -> str:
    """The time of day on a 12-hour clock: ``10:30 pm``."""
    hour = when.hour % 12 or 12
    half = "am" if when.hour < 12 else "pm"
    return f"{hour}:{when.minute:02d} {half}"


def when_text(text: str | None, *, tz: tzinfo, now: datetime) -> str | None:
    """An ISO 8601 time as a person reads it, in ``tz``: ``10:30 pm today``,
    ``8:00 pm tomorrow``, or ``Mon 9 Nov, 8:00 pm``. None when ``text`` is
    None or not a time with an offset."""
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    local = moment.astimezone(tz)
    today = now.astimezone(tz).date()
    if local.date() == today:
        return f"{clock_time(local)} today"
    if local.date() == today + timedelta(days=1):
        return f"{clock_time(local)} tomorrow"
    if local.date() == today - timedelta(days=1):
        return f"{clock_time(local)} yesterday"
    day = f"{local:%a} {local.day} {local:%b}"
    if local.year != today.year:
        day += f" {local.year}"
    return f"{day}, {clock_time(local)}"


def _reset_text(window: LimitsWindow, *, tz: tzinfo, now: datetime) -> str:
    human = when_text(window.resets_at, tz=tz, now=now)
    if window.reset:
        at = f" at {human}" if human else ""
        return f"reset{at} — updates on the next model call"
    return f"resets {human}" if human else ""


def _badge(css: str, status: str | None) -> str:
    if status == "rejected":
        return '<span class="j-badge bad">blocked</span>'
    if css == "crit":
        return '<span class="j-badge bad">critical</span>'
    if css == "warn":
        return '<span class="j-badge warn">high</span>'
    return ""


def _bar(label: str, used_pct: float, value_text: str) -> str:
    """The bar: a ``role=meter`` box with the values as text, and an SVG
    that draws the fill. The SVG uses attributes only, so the strict CSP
    of the viewer does not block it."""
    fill = max(0.0, min(used_pct, 100.0))
    return (
        f'<div class=limit-bar role=meter aria-label="{escape(label)}" '
        f'aria-valuemin=0 aria-valuemax=100 aria-valuenow="{fill:g}" '
        f'aria-valuetext="{escape(value_text)}">'
        '<svg viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">'
        '<rect class=track width="100" height="8" rx="2"/>'
        f'<rect class=fill width="{fill:g}" height="8" rx="2"/></svg></div>'
    )


def _meter(name: str, window: LimitsWindow, *, tz: tzinfo, now: datetime) -> str:
    css = level(window.used_pct, window.status)
    used = pct(window.used_pct)
    badge = ""
    if window.reset:
        css += " reset"
    else:
        badge = _badge(css, window.status)
    return (
        f'<div class="j-card limit {css}">'
        f"<div class=limit-head><span class=limit-name>{escape(name)}</span>"
        f"{badge}"
        f"<span class=limit-value><strong>{used}</strong> used</span></div>"
        f"{_bar(f'{name} used', window.used_pct, f'{used} used')}"
        f"<div class=limit-foot><span>{pct(window.left_pct)} left</span>"
        f"<span>{escape(_reset_text(window, tz=tz, now=now))}</span></div></div>"
    )


def section_html(
    body: LimitsResponse | None, *, configured: bool, tz: tzinfo, now: datetime
) -> str:
    """The limits section of the usage page. ``body`` None means core did
    not answer, or answered 404 or an unknown shape."""
    head = "<section class=limits><h2>Subscription limits</h2>"
    if not configured:
        return (
            f"{head}<p class=j-empty>The limits are not configured. They need "
            f"<code>{CORE_URL_ENV}</code>.</p></section>"
        )
    if body is None:
        return f"{head}<p class=j-empty>The limits are not available now.</p></section>"
    if not body.available:
        reason = f": {escape(body.reason)}" if body.reason else ""
        return f"{head}<p class=j-empty>The limits are not available{reason}.</p></section>"
    meters = [
        _meter(name, window, tz=tz, now=now)
        for name, window in (("5-hour", body.five_hour), ("Weekly", body.seven_day))
        if window is not None
    ]
    if not meters:
        return f"{head}<p class=j-empty>The limits are not available now.</p></section>"
    seen = when_text(body.observed_at, tz=tz, now=now)
    note = f'<p class="j-meta">as of {escape(seen)}</p>' if seen else ""
    return f"{head}{note}<div class=limits-grid>{''.join(meters)}</div></section>"
