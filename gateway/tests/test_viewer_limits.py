"""Offline tests for the subscription limits on the viewer's usage page.

A MockTransport stands in for core, so no network is needed. Every test fixes
the clock, so a reset time reads the same on every date.
"""

from __future__ import annotations

import copy
import textwrap
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from joshua_gateway import viewer
from joshua_gateway import viewer_limits as limits_mod
from joshua_gateway import viewer_usage as usage_mod
from joshua_shared import config
from joshua_shared.contracts import LimitsResponse
from starlette.testclient import TestClient

ALEX = ("alex", "alex-secret")
VIEWER_TOKEN = "viewer-token"
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 7, 22, 20, tzinfo=NY)

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
    """)

# The answer of a real core, while Anthropic blocked the usage endpoint.
REAL: dict[str, Any] = {
    "available": True,
    "reason": None,
    "source": "sdk_event",
    "observed_at": "2026-11-07T22:14:03-05:00",
    "stale": True,
    "five_hour": {
        "used_pct": 61.0,
        "left_pct": 39.0,
        "resets_at": "2026-11-07T22:30:00-05:00",
        "status": None,
    },
    "seven_day": {
        "used_pct": 28.0,
        "left_pct": 72.0,
        "resets_at": "2026-11-09T20:00:00-05:00",
        "status": None,
    },
    "scoped": [],
    "extra_usage": None,
    "retry_after": 2592,
}

USAGE: dict[str, Any] = {
    "timezone": "America/New_York",
    "days": 30,
    "groups": ["chat"],
    "rows": [
        {
            "date": "2026-11-07",
            "groups": {
                "chat": {
                    "calls": 3,
                    "input": 100,
                    "output": 20,
                    "cache_read": 0,
                    "cache_write": 0,
                    "total": 120,
                    "workers": {},
                }
            },
        }
    ],
    "totals": {
        "chat": {
            "calls": 3,
            "input": 100,
            "output": 20,
            "cache_read": 0,
            "cache_write": 0,
            "total": 120,
            "workers": {},
        }
    },
}


@pytest.fixture
def data(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "data"
    (root / "wiki").mkdir(parents=True)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(root))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
    monkeypatch.setenv(usage_mod.CORE_URL_ENV, "http://core")
    monkeypatch.setenv(usage_mod.VIEWER_TOKEN_ENV, VIEWER_TOKEN)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    monkeypatch.setattr(viewer, "_now", lambda tz: NOW.astimezone(tz))
    monkeypatch.setattr(usage_mod, "_today", lambda tz: date(2026, 11, 7))
    return root


class FakeCore:
    """Core with both routes. ``limits`` is the limits answer: a dict, a
    status code, the text ``"bad json"``, or ``"down"`` for no connection."""

    def __init__(self, limits: Any = None) -> None:
        self.limits = REAL if limits is None else limits
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == usage_mod.USAGE_PATH:
            return httpx.Response(200, json=USAGE)
        if self.limits == "down":
            raise httpx.ConnectError("down", request=request)
        if self.limits == "bad json":
            return httpx.Response(200, content=b"{not json")
        if isinstance(self.limits, int):
            return httpx.Response(self.limits, json={"detail": "Not Found"})
        return httpx.Response(200, json=self.limits)

    def limits_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == limits_mod.LIMITS_PATH]


def _with_core(monkeypatch, core: FakeCore) -> None:
    """Keep the env logic of both ``from_env`` functions, and add the fake
    transport to each client they make."""
    transport = httpx.MockTransport(core.handler)
    real_usage = usage_mod.from_env
    real_limits = limits_mod.from_env

    def usage_from_env(env):
        client = real_usage(env)
        if client is None:
            return None
        return usage_mod.CoreUsage(client._base_url, client._token, transport=transport)

    def limits_from_env(env):
        client = real_limits(env)
        if client is None:
            return None
        return limits_mod.CoreLimits(client._base_url, transport=transport)

    monkeypatch.setattr(usage_mod, "from_env", usage_from_env)
    monkeypatch.setattr(limits_mod, "from_env", limits_from_env)


def _get(monkeypatch, limits: Any = None) -> tuple[str, FakeCore]:
    core = FakeCore(limits)
    _with_core(monkeypatch, core)
    response = TestClient(viewer.build_app()).get("/usage", auth=ALEX)
    assert response.status_code == 200
    return response.text, core


def _limits(html: str) -> str:
    return html[html.index("<section class=limits>") : html.index("</section>")]


def _usage_intact(html: str) -> None:
    assert "<h2>Tokens by day</h2>" in html
    assert '<svg viewBox="0 0 640' in html
    assert usage_mod.exact(120) in html


# -- the page -------------------------------------------------------------------


def test_the_real_answer_gives_two_meters_above_the_usage(data, monkeypatch) -> None:
    html, core = _get(monkeypatch)
    section = _limits(html)
    assert html.index("Subscription limits") < html.index("<h2>Tokens by day</h2>")
    assert section.count('<div class="limit ') == 2
    assert "<span class=limit-name>5-hour</span>" in section
    assert "<span class=limit-name>Weekly</span>" in section
    assert "<strong>61%</strong> used" in section and "39% left" in section
    assert "<strong>28%</strong> used" in section and "72% left" in section
    assert "resets 10:30 pm today" in section
    assert "resets Mon 9 Nov, 8:00 pm" in section
    assert 'role=meter aria-label="5-hour used"' in section
    assert 'aria-valuenow="61"' in section and 'aria-valuetext="61% used"' in section
    assert '<rect class=fill width="61"' in section
    # The stale note says when, and no retry count reaches the reader.
    assert "<p class=meta>as of 10:14 pm today</p>" in section
    assert "2592" not in html and "retry" not in section.lower()
    assert "<script" not in html and "style=" not in section
    _usage_intact(html)
    assert len(core.limits_requests()) == 1


def test_no_bearer_goes_to_the_limits_route(data, monkeypatch) -> None:
    _html, core = _get(monkeypatch)
    [request] = core.limits_requests()
    assert "authorization" not in request.headers
    [usage] = [r for r in core.requests if r.url.path == usage_mod.USAGE_PATH]
    assert usage.headers["authorization"] == f"Bearer {VIEWER_TOKEN}"


def test_the_meters_need_core_url_only(data, monkeypatch) -> None:
    monkeypatch.delenv(usage_mod.VIEWER_TOKEN_ENV)
    html, core = _get(monkeypatch)
    assert _limits(html).count('<div class="limit ') == 2
    assert "The usage page is not configured" in html
    assert [r.url.path for r in core.requests] == [limits_mod.LIMITS_PATH]


def test_without_core_url_the_section_says_so(data, monkeypatch) -> None:
    monkeypatch.delenv(usage_mod.CORE_URL_ENV)
    html, core = _get(monkeypatch)
    assert "The limits are not configured" in _limits(html)
    assert core.requests == []


@pytest.mark.parametrize("failure", [404, 503, "down", "bad json", {"available": "maybe"}])
def test_a_failure_leaves_the_usage_intact(data, monkeypatch, failure) -> None:
    html, _core = _get(monkeypatch, failure)
    section = _limits(html)
    assert "The limits are not available now." in section
    assert '<div class="limit ' not in section
    _usage_intact(html)


def test_unavailable_shows_the_reason_and_no_meter(data, monkeypatch) -> None:
    answer = {"available": False, "reason": "the limits are not known yet"}
    section = _limits(_get(monkeypatch, answer)[0])
    assert "The limits are not available: the limits are not known yet." in section
    assert "limit-bar" not in section


def test_an_unknown_field_is_ignored(data, monkeypatch) -> None:
    answer = copy.deepcopy(REAL)
    answer["new_field"] = 1
    answer["five_hour"]["new_field"] = "x"
    assert _limits(_get(monkeypatch, answer)[0]).count('<div class="limit ') == 2


def test_scoped_and_extra_usage_render_when_present(data, monkeypatch) -> None:
    answer = copy.deepcopy(REAL)
    answer["stale"] = False
    answer["scoped"] = [
        {"name": "Fable", "used_pct": 13.0, "left_pct": 87.0, "resets_at": None},
    ]
    answer["extra_usage"] = {"enabled": True, "used": 12.5, "limit": 25.0, "currency": "USD"}
    section = _limits(_get(monkeypatch, answer)[0])
    assert section.count('<div class="limit ') == 4
    assert "<span class=limit-name>Fable weekly</span>" in section
    assert "<span class=limit-name>Extra usage</span>" in section
    assert "<strong>$12.50</strong> of $25.00" in section
    assert "$12.50 left" in section and "50% used" in section
    assert "as of" not in section


@pytest.mark.parametrize(
    "extra",
    [
        None,
        {"enabled": False, "used": 1.0, "limit": 25.0, "currency": "USD"},
        {"enabled": True, "used": None, "limit": None, "currency": None},
    ],
)
def test_no_extra_meter_when_off_or_unknown(data, monkeypatch, extra) -> None:
    answer = copy.deepcopy(REAL)
    answer["extra_usage"] = extra
    section = _limits(_get(monkeypatch, answer)[0])
    assert "Extra usage" not in section
    assert section.count('<div class="limit ') == 2


def test_a_hostile_scoped_name_and_reason_are_escaped(data, monkeypatch) -> None:
    evil = "<script>alert(1)</script>"
    answer = copy.deepcopy(REAL)
    answer["scoped"] = [{"name": evil, "used_pct": 1.0, "left_pct": 99.0}]
    html = _get(monkeypatch, answer)[0]
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; weekly" in html
    html = _get(monkeypatch, {"available": False, "reason": evil})[0]
    assert "<script>" not in html and "&lt;script&gt;" in html


# -- thresholds -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("used", "status", "css", "badge"),
    [
        (74.0, None, "ok", ""),
        (74.9, "allowed", "ok", ""),
        (75.0, None, "warn", "high"),
        (89.9, "allowed_warning", "warn", "high"),
        (90.0, None, "crit", "critical"),
        (10.0, "rejected", "crit", "blocked"),
    ],
)
def test_thresholds(data, monkeypatch, used, status, css, badge) -> None:
    answer = copy.deepcopy(REAL)
    answer["five_hour"].update(used_pct=used, left_pct=100 - used, status=status)
    section = _limits(_get(monkeypatch, answer)[0])
    head = section[section.index('<div class="limit ') :]
    assert head.startswith(f'<div class="limit {css}">')
    if badge:
        assert f"<span class=badge>{badge}</span>" in head.split("Weekly")[0]
    else:
        assert "class=badge" not in head.split("Weekly")[0]


def test_level() -> None:
    assert limits_mod.level(74.99) == "ok"
    assert limits_mod.level(75) == "warn"
    assert limits_mod.level(90) == "crit"
    assert limits_mod.level(0, "rejected") == "crit"
    assert limits_mod.level(80, "normal") == "warn"


# -- the parts --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-07T22:30:00-05:00", "10:30 pm today"),
        ("2026-11-08T03:30:00Z", "10:30 pm today"),
        ("2026-11-08T00:05:00-05:00", "12:05 am tomorrow"),
        ("2026-11-06T12:00:00-05:00", "12:00 pm yesterday"),
        ("2026-11-09T20:00:00.842614-05:00", "Mon 9 Nov, 8:00 pm"),
        ("2027-01-04T09:00:00-05:00", "Mon 4 Jan 2027, 9:00 am"),
        ("2026-11-09T20:00:00", None),
        ("not a time", None),
        (None, None),
    ],
)
def test_when_text(text, expected) -> None:
    assert limits_mod.when_text(text, tz=NY, now=NOW) == expected


def test_when_text_uses_the_instance_zone() -> None:
    # In Auckland the reset and the clock are both on 8 November, 18 hours ahead.
    akl = ZoneInfo("Pacific/Auckland")
    assert limits_mod.when_text("2026-11-07T22:30:00-05:00", tz=akl, now=NOW) == "4:30 pm today"


def test_a_past_reset_reads_reset() -> None:
    body = LimitsResponse.model_validate(
        {
            "available": True,
            "five_hour": {
                "used_pct": 5,
                "left_pct": 95,
                "resets_at": (NOW - timedelta(minutes=10)).isoformat(),
            },
        }
    )
    html = limits_mod.section_html(body, configured=True, tz=NY, now=NOW)
    assert "reset 10:10 pm today" in html and "resets" not in html


def test_an_answer_with_no_window_is_not_available() -> None:
    body = LimitsResponse(available=True)
    html = limits_mod.section_html(body, configured=True, tz=NY, now=NOW)
    assert "not available now" in html


def test_a_stale_answer_with_no_time_says_old_values() -> None:
    body = LimitsResponse.model_validate({**REAL, "observed_at": None})
    html = limits_mod.section_html(body, configured=True, tz=NY, now=NOW)
    assert "<p class=meta>old values</p>" in html


def test_numbers_and_money() -> None:
    assert limits_mod.pct(61.0) == "61%"
    assert limits_mod.pct(61.25) == "61.2%"
    assert limits_mod.money(1234.5, "usd") == "$1,234.50"
    assert limits_mod.money(3, None) == "$3.00"
    assert limits_mod.money(3, "CHF") == "3.00 CHF"
    assert limits_mod.clock_time(datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) == "12:00 am"
    assert limits_mod.clock_time(datetime(2026, 1, 1, 13, 7, tzinfo=UTC)) == "1:07 pm"


def test_a_bar_is_held_to_0_100() -> None:
    body = LimitsResponse.model_validate(
        {"available": True, "five_hour": {"used_pct": 130, "left_pct": -30}}
    )
    html = limits_mod.section_html(body, configured=True, tz=NY, now=NOW)
    assert '<rect class=fill width="100"' in html and "<strong>130%</strong>" in html


def test_zone_falls_back_to_utc() -> None:
    assert limits_mod.zone("Not/AZone") is UTC
    assert limits_mod.zone("America/New_York") == NY


def test_from_env_needs_core_url_only() -> None:
    assert limits_mod.from_env({}) is None
    assert isinstance(limits_mod.from_env({"CORE_URL": "http://core"}), limits_mod.CoreLimits)


async def test_fetch_gives_none_on_a_failure() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = limits_mod.CoreLimits("http://core", transport=httpx.MockTransport(boom))
    assert await client.fetch() is None


def test_now_is_aware_in_the_zone() -> None:
    tz = timezone(timedelta(hours=-5))
    assert viewer._now(tz).utcoffset() == timedelta(hours=-5)
