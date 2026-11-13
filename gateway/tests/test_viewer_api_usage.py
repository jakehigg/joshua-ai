"""Offline tests for the JSON route of the usage page.

``GET /api/v1/usage?days=<n>`` makes both calls to core at the same time: the
tokens of each group by day, and the subscription limits. A MockTransport
stands in for core, so no network is needed. The route answers 200 with no
core, with a call that fails, and with a ``404`` from the limits route. Every
test fixes the clock, so a reset time reads the same on every date.
"""

from __future__ import annotations

import copy
import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from conftest import core_stamp, signed_in
from joshua_gateway import viewer
from joshua_gateway import viewer_limits as limits_mod
from joshua_gateway import viewer_usage as usage_mod
from joshua_gateway.viewer_api import usage as usage_api
from joshua_shared import config
from joshua_shared.contracts import LimitsResponse, UsageResponse
from starlette.testclient import TestClient

ALEX = signed_in("alex")
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


def _counts(calls: int, input_: int, output: int, **kw: Any) -> dict[str, Any]:
    """The counts of one group or one worker. ``models`` and ``workers`` are
    in the dict only when a test names them, because a worker holds no worker
    of its own."""
    row = {
        "calls": calls,
        "input": input_,
        "output": output,
        "cache_read": 0,
        "cache_write": 0,
        "total": input_ + output,
    }
    return {**row, **kw}


# Core's usage: one day, a group with workers, and a group with no usage.
USAGE: dict[str, Any] = {
    "timezone": "America/New_York",
    "days": 30,
    "groups": ["chat", "reflection", "extra"],
    "rows": [
        {
            "date": "2026-11-07",
            "groups": {
                "chat": _counts(3, 100, 20, models=["m-chat"]),
                "extra": _counts(5, 7000, 900, workers={"editor": _counts(1, 1000, 800)}),
            },
        }
    ],
    "totals": {
        "chat": _counts(3, 100, 20, models=["m-chat"]),
        "reflection": _counts(0, 0, 0),
        "extra": _counts(5, 7000, 900, workers={"editor": _counts(1, 1000, 800)}),
    },
}

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


@pytest.fixture
def data(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "data"
    (root / "wiki").mkdir(parents=True)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(root))
    monkeypatch.setenv(usage_mod.CORE_URL_ENV, "http://core")
    monkeypatch.setenv(usage_mod.VIEWER_TOKEN_ENV, VIEWER_TOKEN)
    core_stamp(monkeypatch)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    monkeypatch.setattr(viewer, "_now", lambda tz: NOW.astimezone(tz))
    return root


class FakeCore:
    """Core with both routes. ``limits`` is the limits answer: a dict, a
    status code, the text ``"bad json"``, or ``"down"`` for no connection.
    ``usage`` is the usage answer, or a status code."""

    def __init__(self, limits: Any = None, usage: Any = None) -> None:
        self.limits = REAL if limits is None else limits
        self.usage = USAGE if usage is None else usage
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == usage_mod.USAGE_PATH:
            if isinstance(self.usage, int):
                return httpx.Response(self.usage, json={"error": "no"})
            return httpx.Response(200, json=self.usage)
        if self.limits == "down":
            raise httpx.ConnectError("down", request=request)
        if self.limits == "bad json":
            return httpx.Response(200, content=b"{not json")
        if isinstance(self.limits, int):
            return httpx.Response(self.limits, json={"detail": "Not Found"})
        return httpx.Response(200, json=self.limits)

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def of(self, path: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == path]


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


def _get(monkeypatch, core: FakeCore | None = None, **params: Any) -> dict[str, Any]:
    _with_core(monkeypatch, core if core is not None else FakeCore())
    response = TestClient(viewer.build_app()).get("/api/v1/usage", params=params, auth=ALEX)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body: dict[str, Any] = response.json()
    return body


# -- both calls -----------------------------------------------------------------


def test_both_calls_answer_the_meters_and_core_usage_unchanged(data, monkeypatch) -> None:
    core = FakeCore()
    body = _get(monkeypatch, core)
    assert body["days"] == 30
    assert body["timezone"] == "America/New_York"
    assert body["now"].startswith("2026-11-07T22:20:00")
    # The usage is core's own body, as core sent it.
    assert body["usage_configured"] is True
    assert body["usage"] == UsageResponse.model_validate(USAGE).model_dump()
    # Every number of a meter is derived here.
    assert body["limits_configured"] is True
    assert body["limits"]["as_of"] == "10:14 pm today"
    assert body["limits"]["reason"] is None
    five, weekly = body["limits"]["windows"]
    assert five == {
        "name": "5-hour",
        "pct_used": "61%",
        "pct_left": "39%",
        "fill": 61.0,
        "level": "ok",
        "badge": "",
        "reset_at": "2026-11-07T22:30:00-05:00",
        "reset_text": "resets 10:30 pm today",
        "reset": False,
    }
    assert weekly["name"] == "Weekly"
    assert weekly["pct_used"] == "28%"
    assert weekly["reset_text"] == "resets Mon 9 Nov, 8:00 pm"
    assert sorted(core.paths()) == sorted([usage_mod.USAGE_PATH, limits_mod.LIMITS_PATH])


def test_the_usage_call_carries_the_fleet_token_and_the_limits_call_none(data, monkeypatch) -> None:
    core = FakeCore()
    _get(monkeypatch, core)
    [usage_call] = core.of(usage_mod.USAGE_PATH)
    assert usage_call.headers["authorization"] == f"Bearer {VIEWER_TOKEN}"
    assert not any(key.lower().startswith("x-joshua") for key in usage_call.headers)
    [limits_call] = core.of(limits_mod.LIMITS_PATH)
    assert "authorization" not in limits_call.headers


def test_the_route_needs_a_sign_in(data) -> None:
    response = TestClient(viewer.build_app()).get("/api/v1/usage")
    assert response.status_code == 401
    assert "WWW-Authenticate" not in response.headers


# -- what is missing ------------------------------------------------------------


def test_without_core_url_both_halves_are_null(data, monkeypatch) -> None:
    monkeypatch.delenv(usage_mod.CORE_URL_ENV)
    core = FakeCore()
    body = _get(monkeypatch, core)
    assert body["usage_configured"] is False and body["usage"] is None
    assert body["limits_configured"] is False and body["limits"] is None
    assert body["days"] == 30 and body["timezone"] == "America/New_York"
    assert core.requests == []


def test_the_limits_need_core_url_only(data, monkeypatch) -> None:
    monkeypatch.delenv(usage_mod.VIEWER_TOKEN_ENV)
    core = FakeCore()
    body = _get(monkeypatch, core)
    assert body["usage_configured"] is False and body["usage"] is None
    assert len(body["limits"]["windows"]) == 2
    assert core.paths() == [limits_mod.LIMITS_PATH]


def test_a_usage_call_that_fails_keeps_the_meters(data, monkeypatch) -> None:
    body = _get(monkeypatch, FakeCore(usage=503))
    assert body["usage_configured"] is True and body["usage"] is None
    assert len(body["limits"]["windows"]) == 2


def test_an_unknown_usage_shape_is_null(data, monkeypatch) -> None:
    body = _get(monkeypatch, FakeCore(usage={"rows": "nope"}))
    assert body["usage"] is None


@pytest.mark.parametrize("failure", [404, 503, "down", "bad json", {"available": "maybe"}])
def test_a_limits_failure_keeps_the_usage(data, monkeypatch, failure) -> None:
    """``404`` is a core with ``limits.enabled`` false."""
    body = _get(monkeypatch, FakeCore(limits=failure))
    assert body["limits_configured"] is True and body["limits"] is None
    assert body["usage"]["rows"][0]["date"] == "2026-11-07"


def test_limits_that_are_not_available_carry_the_reason_and_no_window(data, monkeypatch) -> None:
    answer = {"available": False, "reason": "the limits are not known yet"}
    body = _get(monkeypatch, FakeCore(limits=answer))
    assert body["limits"] == {
        "windows": [],
        "as_of": None,
        "reason": "the limits are not known yet",
    }


def test_an_unknown_field_of_the_limits_is_ignored(data, monkeypatch) -> None:
    answer = copy.deepcopy(REAL)
    answer["new_field"] = 1
    answer["five_hour"]["new_field"] = "x"
    assert len(_get(monkeypatch, FakeCore(limits=answer))["limits"]["windows"]) == 2


def test_a_name_of_core_is_data_and_not_html(data, monkeypatch) -> None:
    evil = "<script>alert(1)</script>"
    answer = {
        "timezone": "UTC",
        "days": 30,
        "groups": [evil],
        "rows": [{"date": "2026-11-07", "groups": {evil: _counts(1, 5, 5)}}],
        "totals": {evil: _counts(1, 5, 5)},
    }
    body = _get(monkeypatch, FakeCore(usage=answer))
    # The page escapes by construction, so the name goes on as core sent it.
    assert body["usage"]["groups"] == [evil]
    assert body["usage"]["totals"][evil]["total"] == 10


# -- the window -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "days"),
    [("7", 7), ("x", 30), ("", 30), ("0", 1), ("9999", 366)],
)
def test_the_days_query_is_held_to_the_window_core_allows(data, monkeypatch, raw, days) -> None:
    core = FakeCore()
    body = _get(monkeypatch, core, days=raw)
    assert body["days"] == days
    [usage_call] = core.of(usage_mod.USAGE_PATH)
    assert usage_call.url.params["days"] == str(days)


# -- the meters -----------------------------------------------------------------


def test_a_reset_window_keeps_its_numbers_and_says_the_next_call_updates_it(
    data, monkeypatch
) -> None:
    answer = copy.deepcopy(REAL)
    answer["five_hour"].update(
        used_pct=95.0, left_pct=5.0, resets_at="2026-11-07T21:30:00-05:00", reset=True
    )
    five, weekly = _get(monkeypatch, FakeCore(limits=answer))["limits"]["windows"]
    assert five["reset"] is True and five["level"] == "crit"
    assert five["fill"] == 95.0 and five["pct_used"] == "95%"
    # The window carries no badge, because its numbers are old.
    assert five["badge"] == ""
    assert five["reset_text"] == "reset at 9:30 pm today — updates on the next model call"
    assert weekly["reset_text"] == "resets Mon 9 Nov, 8:00 pm"


@pytest.mark.parametrize(
    ("used", "status", "level", "badge"),
    [
        (74.0, None, "ok", ""),
        (74.9, "allowed", "ok", ""),
        (75.0, None, "warn", "high"),
        (89.9, "allowed_warning", "warn", "high"),
        (90.0, None, "crit", "critical"),
        (10.0, "rejected", "crit", "blocked"),
    ],
)
def test_the_level_and_the_badge_of_a_meter(data, monkeypatch, used, status, level, badge) -> None:
    answer = copy.deepcopy(REAL)
    answer["five_hour"].update(used_pct=used, left_pct=100 - used, status=status)
    [five, _weekly] = _get(monkeypatch, FakeCore(limits=answer))["limits"]["windows"]
    assert five["level"] == level and five["badge"] == badge


def test_a_bar_is_held_to_0_100() -> None:
    body = LimitsResponse.model_validate(
        {"available": True, "five_hour": {"used_pct": 130, "left_pct": -30}}
    )
    [five] = usage_api.limits_json(body, tz=NY, now=NOW)["windows"]
    assert five["fill"] == 100.0 and five["pct_used"] == "130%"
    assert five["reset_text"] == "" and five["reset_at"] is None


def test_a_reset_window_with_no_time_says_reset_only() -> None:
    body = LimitsResponse.model_validate(
        {
            "available": True,
            "observed_at": "2026-11-07T20:00:00-05:00",
            "seven_day": {"used_pct": 40, "left_pct": 60, "reset": True},
        }
    )
    answer = usage_api.limits_json(body, tz=NY, now=NOW)
    assert answer["as_of"] == "8:00 pm today"
    [weekly] = answer["windows"]
    assert weekly["name"] == "Weekly"
    assert weekly["reset_text"] == "reset — updates on the next model call"


def test_the_reset_flag_and_not_the_clock_decides() -> None:
    body = LimitsResponse.model_validate(
        {
            "available": True,
            "five_hour": {
                "used_pct": 5,
                "left_pct": 95,
                "resets_at": (NOW - timedelta(minutes=10)).isoformat(),
                "reset": False,
            },
        }
    )
    [five] = usage_api.limits_json(body, tz=NY, now=NOW)["windows"]
    assert five["reset"] is False
    assert five["reset_text"] == "resets 10:10 pm today"


def test_an_answer_with_no_window_carries_no_meter() -> None:
    answer = usage_api.limits_json(LimitsResponse(available=True), tz=NY, now=NOW)
    assert answer == {"windows": [], "as_of": None, "reason": None}


def test_no_answer_is_null() -> None:
    assert usage_api.limits_json(None, tz=NY, now=NOW) is None
