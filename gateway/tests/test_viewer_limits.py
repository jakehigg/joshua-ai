"""Offline tests for the client of the subscription limits, and its numbers.

A MockTransport stands in for core, so no network is needed. The route that
calls this client is ``viewer_api.usage`` (`test_viewer_api_usage.py`), and
the meters are React. Every test fixes the clock, so a reset time reads the
same on every date.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from joshua_gateway import viewer
from joshua_gateway import viewer_limits as limits_mod

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 7, 22, 20, tzinfo=NY)

# The answer of a real core after a model call on the subscription.
REAL: dict[str, Any] = {
    "available": True,
    "reason": None,
    "observed_at": "2026-11-07T22:14:03-05:00",
    "five_hour": {
        "used_pct": 61.0,
        "left_pct": 39.0,
        "resets_at": "2026-11-07T22:30:00-05:00",
        "status": None,
        "reset": False,
    },
    "seven_day": {
        "used_pct": 28.0,
        "left_pct": 72.0,
        "resets_at": "2026-11-09T20:00:00-05:00",
        "status": None,
        "reset": False,
    },
}


# -- the call -------------------------------------------------------------------


async def test_the_call_sends_no_bearer_and_reads_both_windows() -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=REAL)

    transport = httpx.MockTransport(answer)
    body = await limits_mod.CoreLimits("http://core", transport=transport).fetch()
    assert body is not None and body.available is True
    assert body.five_hour is not None and body.five_hour.used_pct == 61.0
    assert body.seven_day is not None and body.seven_day.left_pct == 72.0
    [request] = sent
    assert request.url.path == limits_mod.LIMITS_PATH
    assert "authorization" not in request.headers


@pytest.mark.parametrize("status", [404, 503])
async def test_a_refused_call_is_none(status) -> None:
    """``404`` is a core with ``limits.enabled`` false."""

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "Not Found"})

    transport = httpx.MockTransport(refuse)
    assert await limits_mod.CoreLimits("http://core", transport=transport).fetch() is None


async def test_a_network_error_is_none() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = limits_mod.CoreLimits("http://core", transport=httpx.MockTransport(boom))
    assert await client.fetch() is None


async def test_an_unknown_shape_is_none_and_an_unknown_field_is_ignored() -> None:
    def odd(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"available": "maybe"})

    client = limits_mod.CoreLimits("http://core", transport=httpx.MockTransport(odd))
    assert await client.fetch() is None

    def newer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**REAL, "new_field": 1})

    client = limits_mod.CoreLimits("http://core", transport=httpx.MockTransport(newer))
    assert await client.fetch() is not None


def test_from_env_needs_core_url_only() -> None:
    assert limits_mod.from_env({}) is None
    assert isinstance(limits_mod.from_env({"CORE_URL": "http://core"}), limits_mod.CoreLimits)


# -- thresholds -----------------------------------------------------------------


def test_level() -> None:
    assert limits_mod.level(74.99) == "ok"
    assert limits_mod.level(75) == "warn"
    assert limits_mod.level(90) == "crit"
    assert limits_mod.level(0, "rejected") == "crit"
    assert limits_mod.level(80, "normal") == "warn"


# -- the numbers of a meter -----------------------------------------------------


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


def test_numbers() -> None:
    assert limits_mod.pct(61.0) == "61%"
    assert limits_mod.pct(61.25) == "61.2%"
    assert limits_mod.clock_time(datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) == "12:00 am"
    assert limits_mod.clock_time(datetime(2026, 1, 1, 13, 7, tzinfo=UTC)) == "1:07 pm"


def test_zone_falls_back_to_utc() -> None:
    assert limits_mod.zone("Not/AZone") is UTC
    assert limits_mod.zone("America/New_York") == NY


def test_now_is_aware_in_the_zone() -> None:
    tz = timezone(timedelta(hours=-5))
    assert viewer._now(tz).utcoffset() == timedelta(hours=-5)
