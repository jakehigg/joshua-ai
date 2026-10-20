"""The usage page of the viewer: one call to core, and its HTML.

Core keeps a row for each model call in ``usage_event``. The viewer asks
core's ``GET /v1/usage`` with its own fleet token and shows the tokens of each
group by day: ``chat``, ``reflection``, ``internet``, ``describe``, and one
group for each job that is on. The page has a bar chart of the total tokens of
each day, stacked by group, a table of the days by group, a table of the
totals and the models of each group, a table of the workers of each job, and
the counts of each day. Every group of ``groups`` is in the tables, also with
no usage.

The viewer needs ``CORE_URL`` and ``JOSHUA_TOKEN_VIEWER``. With either one
missing, :func:`from_env` returns None and the page says so. A call that
fails returns None too, and the page says so. The page answers 200 in both
cases.

Every group name, worker name, model, and date comes from core, and the HTML
escapes each one. The
chart is inline SVG with no script, and each color comes from a class in the
viewer's stylesheet, so the page loads nothing more.

The functions here build data and HTML only. The route lives in ``viewer``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from html import escape
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from joshua_shared.contracts import (
    USAGE_DEFAULT_DAYS,
    USAGE_MAX_DAYS,
    UsageCounts,
    UsageResponse,
)
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger
from pydantic import ValidationError

logger = get_logger("viewer.usage")

CORE_URL_ENV = "CORE_URL"
VIEWER_TOKEN_ENV = "JOSHUA_TOKEN_VIEWER"
USAGE_PATH = "/v1/usage"

# A person waits for this page, so a slow core must not hold it open.
TIMEOUT_S = 5.0

# The windows the page offers as links.
WINDOWS = (7, 30, 90, 366)

# The number of series colors in the stylesheet (``.s0`` to ``.s7``).
SERIES_COLORS = 8

_COLUMNS = (
    ("calls", "Calls"),
    ("input", "Input"),
    ("output", "Output"),
    ("cache_read", "Cache read"),
    ("cache_write", "Cache write"),
    ("total", "Total"),
)

# The chart, in SVG user units.
_CHART_W = 640
_CHART_H = 220
_PAD_L = 48
_PAD_R = 8
_PAD_T = 12
_PAD_B = 28


class CoreUsage:
    """Read the usage by day from core."""

    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._transport = transport

    async def fetch(self, days: int) -> UsageResponse | None:
        """Return core's answer, or None when core cannot answer."""
        try:
            async with FleetClient(
                self._base_url, self._token, TIMEOUT_S, transport=self._transport
            ) as client:
                response = await client.get_json(USAGE_PATH, params={"days": days})
        except httpx.HTTPError as exc:
            logger.warning({"message": "core usage failed", "error": type(exc).__name__})
            return None
        if response.status_code != 200:
            logger.warning({"message": "core usage refused", "status": response.status_code})
            return None
        try:
            return UsageResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            logger.warning({"message": "core usage answered an unknown shape"})
            return None


def from_env(env: Mapping[str, str]) -> CoreUsage | None:
    """A client from ``CORE_URL`` and ``JOSHUA_TOKEN_VIEWER``, or None."""
    base_url = env.get(CORE_URL_ENV)
    token = env.get(VIEWER_TOKEN_ENV)
    if not base_url or not token:
        return None
    return CoreUsage(base_url, token)


def parse_days(raw: str | None) -> int:
    """The ``days`` query value held to ``1..USAGE_MAX_DAYS``. A value that is
    not an integer gives the default."""
    try:
        value = int(raw) if raw else USAGE_DEFAULT_DAYS
    except ValueError:
        value = USAGE_DEFAULT_DAYS
    return max(1, min(value, USAGE_MAX_DAYS))


def exact(value: int) -> str:
    """An exact integer with a thin space between each group of three digits."""
    return f"{value:,}".replace(",", " ")


def short(value: int) -> str:
    """A count for a chart label: ``950``, ``12k``, ``3.4M``."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(value)


def _sum(items: list[UsageCounts]) -> UsageCounts:
    return UsageCounts(**{name: sum(getattr(item, name) for item in items) for name, _ in _COLUMNS})


def _series(groups: list[str]) -> dict[str, str]:
    """The color class of each group, by its place in the sorted list."""
    return {name: f"s{index % SERIES_COLORS}" for index, name in enumerate(groups)}


def _windows(days: int) -> str:
    links = []
    for window in WINDOWS:
        label = f"{window} days"
        if window == days:
            links.append(f'<span class="chip on">{label}</span>')
        else:
            href = "/usage?" + urlencode({"days": window})
            links.append(f'<a class=chip href="{escape(href)}">{label}</a>')
    return f"<p class=filters>{''.join(links)}</p>"


def _parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _today(timezone: str) -> date:
    """Today in ``timezone``; in UTC when the name is not a zone."""
    try:
        return datetime.now(ZoneInfo(timezone)).date()
    except (KeyError, ValueError):
        return datetime.now(UTC).date()


def chart_svg(body: UsageResponse, *, today: date | None = None) -> str:
    """A bar for each day of the window, today included, stacked by group.

    The window is ``body.days`` days back from today in ``body.timezone``, so
    a longer window shows a longer axis also when the usage stops short of it.
    A day with no usage is an empty slot, and a window with no usage at all
    is an empty chart that says so.
    """
    series = _series(body.groups)
    by_day = {row.date: row for row in body.rows}
    last = today or _today(body.timezone)
    span = max(1, min(body.days, USAGE_MAX_DAYS))
    first = last - timedelta(days=span - 1)
    days = [(first + timedelta(days=n)).isoformat() for n in range(span)]
    totals = [sum(g.total for g in by_day[d].groups.values()) if d in by_day else 0 for d in days]
    peak = max(totals) or 1
    plot_w = _CHART_W - _PAD_L - _PAD_R
    plot_h = _CHART_H - _PAD_T - _PAD_B
    slot = plot_w / len(days)
    bar = max(slot * 0.8, 1.0)
    parts: list[str] = []
    for tick in (0, peak // 2, peak):
        y = _PAD_T + plot_h - plot_h * tick / peak
        parts.append(
            f'<line class=grid x1="{_PAD_L}" x2="{_CHART_W - _PAD_R}" '
            f'y1="{y:.1f}" y2="{y:.1f}"/>'
            f'<text class=axis x="{_PAD_L - 6}" y="{y + 4:.1f}" '
            f'text-anchor="end">{escape(short(tick))}</text>'
        )
    for index, day in enumerate(days):
        row = by_day.get(day)
        if row is None:
            continue
        x = _PAD_L + index * slot + (slot - bar) / 2
        base = _PAD_T + plot_h
        for name in body.groups:
            group = row.groups.get(name)
            if group is None or group.total <= 0:
                continue
            height = plot_h * group.total / peak
            base -= height
            label = f"{day} · {name}: {exact(group.total)} tokens"
            parts.append(
                f'<rect class="{series[name]}" x="{x:.1f}" y="{base:.1f}" '
                f'width="{bar:.1f}" height="{height:.1f}"><title>{escape(label)}</title></rect>'
            )
    if not any(totals):
        parts.append(
            f'<text class=axis x="{_CHART_W / 2:.0f}" y="{_PAD_T + plot_h / 2:.0f}" '
            'text-anchor="middle">no usage in this window</text>'
        )
    bottom = _CHART_H - 8
    parts.append(
        f'<text class=axis x="{_PAD_L}" y="{bottom}">{escape(days[0])}</text>'
        f'<text class=axis x="{_CHART_W - _PAD_R}" y="{bottom}" '
        f'text-anchor="end">{escape(days[-1])}</text>'
    )
    legend = "".join(
        f'<li><span class="sw {series[name]}"></span>{escape(name)}</li>' for name in body.groups
    )
    return (
        "<figure class=usage-chart>"
        f'<svg viewBox="0 0 {_CHART_W} {_CHART_H}" role="img" '
        'aria-label="Total tokens per day, stacked by group">'
        f"{''.join(parts)}</svg>"
        f"<ul class=legend>{legend}</ul></figure>"
    )


def _days_table(body: UsageResponse) -> str:
    """Days down, groups across, the total tokens in each cell."""
    head = "".join(f"<th>{escape(name)}</th>" for name in body.groups)
    rows = []
    for row in body.rows:
        cells = []
        for name in body.groups:
            group = row.groups.get(name)
            cells.append(f"<td class=num>{exact(group.total) if group else ''}</td>")
        day_total = sum(g.total for g in row.groups.values())
        rows.append(
            f"<tr><th>{escape(row.date)}</th>{''.join(cells)}"
            f"<td class=num><strong>{exact(day_total)}</strong></td></tr>"
        )
    totals = "".join(
        f"<td class=num><strong>{exact(body.totals[name].total)}</strong></td>"
        if name in body.totals
        else "<td></td>"
        for name in body.groups
    )
    grand = sum(g.total for g in body.totals.values())
    return (
        "<h2>Tokens by day</h2><table class=usage>"
        f"<thead><tr><th>Date</th>{head}<th>Total</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        f"<tfoot><tr><th>Total</th>{totals}"
        f"<td class=num><strong>{exact(grand)}</strong></td></tr></tfoot></table>"
    )


def models_text(models: list[str]) -> str:
    """The models joined with ``, ``, or ``—`` when there is none. Not escaped."""
    return ", ".join(models) if models else "—"


def _counts_table(
    groups: Mapping[str, UsageCounts],
    *,
    caption: str = "",
    label: str = "Group",
    models: bool = False,
) -> str:
    """One row for each group, the six counts across, and a total row. With
    ``models``, a last column holds the models of each row."""
    head = "".join(f"<th>{name}</th>" for _, name in _COLUMNS)
    if models:
        head += "<th>Models</th>"
    rows = []
    for name, counts in groups.items():
        cells = "".join(f"<td class=num>{exact(getattr(counts, key))}</td>" for key, _ in _COLUMNS)
        if models:
            cells += f"<td>{escape(models_text(counts.models))}</td>"
        rows.append(f"<tr><th>{escape(name)}</th>{cells}</tr>")
    total = _sum(list(groups.values()))
    foot = "".join(
        f"<td class=num><strong>{exact(getattr(total, key))}</strong></td>" for key, _ in _COLUMNS
    )
    if models:
        foot += "<td></td>"
    cap = f"<caption>{escape(caption)}</caption>" if caption else ""
    return (
        f"<table class=usage>{cap}<thead><tr><th>{escape(label)}</th>{head}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        f"<tfoot><tr><th>Total</th>{foot}</tr></tfoot></table>"
    )


def _totals(body: UsageResponse) -> str:
    """The totals of each group of ``groups`` with its models, then one table
    for each job group that has workers."""
    groups = {name: body.totals.get(name, UsageCounts()) for name in body.groups}
    parts = [f"<h2>Totals by group</h2>{_counts_table(groups, models=True)}"]
    for name in body.groups:
        group = body.totals.get(name)
        if group is None or not group.workers:
            continue
        parts.append(
            f"<h3>The workers of {escape(name)}</h3>"
            f"{_counts_table(group.workers, label='Worker', models=True)}"
        )
    return "".join(parts)


def page_html(body: UsageResponse | None, *, days: int, configured: bool) -> str:
    """The body of the usage page. ``body`` None means core did not answer."""
    head = f"<h1>usage</h1>{_windows(days)}"
    if not configured:
        return (
            f"{head}<p class=snippet>The usage page is not configured. It needs "
            f"<code>{CORE_URL_ENV}</code> and <code>{VIEWER_TOKEN_ENV}</code>.</p>"
        )
    if body is None:
        return f"{head}<p class=snippet>Core did not answer. The usage is not available now.</p>"
    intro = (
        f"<p class=meta>Tokens of the last {body.days} days, by calendar day in "
        f"{escape(body.timezone)}. A day with no usage is not in the tables of "
        "days.</p>"
    )
    if not body.rows:
        return f"{head}{intro}{chart_svg(body)}<p>No usage in this window.</p>{_totals(body)}"
    per_day = "".join(
        f"<details class=day><summary>{escape(row.date)}</summary>"
        f"{_counts_table(row.groups)}</details>"
        for row in body.rows
    )
    return (
        f"{head}{intro}{chart_svg(body)}{_days_table(body)}{_totals(body)}"
        f"<h2>Each day</h2>{per_day}"
    )
