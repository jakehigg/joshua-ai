"""The subscription limits on the usage page: one call to core, and its HTML.

The viewer asks core's ``GET /v1/limits`` and shows one meter for each limit
window: the 5-hour window, the weekly window, each weekly limit of one model
(``scoped``), and the extra usage when it is on. A meter shows the percent
used, a bar, the percent left, and the reset time in the instance timezone.

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
from joshua_shared.contracts import (
    LIMITS_PATH,
    LimitsExtraUsage,
    LimitsResponse,
)
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

_CURRENCY_SIGNS = {"USD": "$", "EUR": "€", "GBP": "£"}


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


def _reset_text(text: str | None, *, tz: tzinfo, now: datetime) -> str:
    human = when_text(text, tz=tz, now=now)
    if human is None:
        return ""
    try:
        past = datetime.fromisoformat(text or "") <= now
    except TypeError:
        past = False
    return f"reset {human}" if past else f"resets {human}"


def _badge(css: str, status: str | None) -> str:
    if status == "rejected":
        return "<span class=badge>blocked</span>"
    if css == "crit":
        return "<span class=badge>critical</span>"
    if css == "warn":
        return "<span class=badge>high</span>"
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


def _meter(
    name: str,
    used_pct: float,
    left_pct: float,
    resets_at: str | None,
    *,
    status: str | None,
    tz: tzinfo,
    now: datetime,
) -> str:
    css = level(used_pct, status)
    reset = _reset_text(resets_at, tz=tz, now=now)
    used = pct(used_pct)
    return (
        f'<div class="limit {css}">'
        f"<div class=limit-head><span class=limit-name>{escape(name)}</span>"
        f"{_badge(css, status)}"
        f"<span class=limit-value><strong>{used}</strong> used</span></div>"
        f"{_bar(f'{name} used', used_pct, f'{used} used')}"
        f"<div class=limit-foot><span>{pct(left_pct)} left</span>"
        f"<span>{escape(reset)}</span></div></div>"
    )


def money(amount: float, currency: str | None) -> str:
    """An amount with its currency: ``$12.50``, or ``12.50 CHF``."""
    code = (currency or "USD").upper()
    sign = _CURRENCY_SIGNS.get(code)
    return f"{sign}{amount:,.2f}" if sign else f"{amount:,.2f} {code}"


def _extra(extra: LimitsExtraUsage | None) -> str:
    """The extra usage meter: the amount used of the monthly limit. Nothing
    when the extra usage is off or core does not give both amounts."""
    if extra is None or not extra.enabled or extra.used is None or not extra.limit:
        return ""
    used_pct = 100.0 * extra.used / extra.limit
    css = level(used_pct)
    used = money(extra.used, extra.currency)
    limit = money(extra.limit, extra.currency)
    left = money(max(extra.limit - extra.used, 0.0), extra.currency)
    return (
        f'<div class="limit {css}">'
        f"<div class=limit-head><span class=limit-name>Extra usage</span>"
        f"{_badge(css, None)}"
        f"<span class=limit-value><strong>{escape(used)}</strong> "
        f"of {escape(limit)}</span></div>"
        f"{_bar('Extra usage used', used_pct, f'{used} of {limit} used')}"
        f"<div class=limit-foot><span>{escape(left)} left</span>"
        f"<span>{pct(used_pct)} used</span></div></div>"
    )


def section_html(
    body: LimitsResponse | None, *, configured: bool, tz: tzinfo, now: datetime
) -> str:
    """The limits section of the usage page. ``body`` None means core did
    not answer, or answered 404 or an unknown shape."""
    head = "<section class=limits><h2>Subscription limits</h2>"
    if not configured:
        return (
            f"{head}<p class=snippet>The limits are not configured. They need "
            f"<code>{CORE_URL_ENV}</code>.</p></section>"
        )
    if body is None:
        return f"{head}<p class=snippet>The limits are not available now.</p></section>"
    if not body.available:
        reason = f": {escape(body.reason)}" if body.reason else ""
        return f"{head}<p class=snippet>The limits are not available{reason}.</p></section>"
    meters = []
    if body.five_hour is not None:
        w = body.five_hour
        meters.append(
            _meter("5-hour", w.used_pct, w.left_pct, w.resets_at, status=w.status, tz=tz, now=now)
        )
    if body.seven_day is not None:
        w = body.seven_day
        meters.append(
            _meter("Weekly", w.used_pct, w.left_pct, w.resets_at, status=w.status, tz=tz, now=now)
        )
    for s in body.scoped:
        meters.append(
            _meter(
                f"{s.name} weekly", s.used_pct, s.left_pct, s.resets_at, status=None, tz=tz, now=now
            )
        )
    extra = _extra(body.extra_usage)
    if extra:
        meters.append(extra)
    if not meters:
        return f"{head}<p class=snippet>The limits are not available now.</p></section>"
    note = ""
    if body.stale:
        seen = when_text(body.observed_at, tz=tz, now=now)
        note = f"<p class=meta>as of {escape(seen)}</p>" if seen else "<p class=meta>old values</p>"
    return f"{head}{note}<div class=limits-grid>{''.join(meters)}</div></section>"
