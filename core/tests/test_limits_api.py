"""`GET /v1/limits`: open with no bearer, 404 when `limits.enabled` is off,
and the answer of the tracker."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from joshua_core.limits import LimitsTracker
from joshua_core.limits_api import build_limits_router
from joshua_shared.config import LimitsConfig
from joshua_shared.contracts import LIMITS_PATH, LimitsResponse
from starlette.testclient import TestClient
from test_limits import RAW_EVENT, TOKEN, Clock, event


def _client(*, enabled: bool = True, tracker: LimitsTracker | None = None) -> TestClient:
    app = FastAPI()
    settings = SimpleNamespace(limits=LimitsConfig(enabled=enabled))
    app.state.ctx = SimpleNamespace(settings=settings, limits=tracker)
    app.include_router(build_limits_router())
    return TestClient(app)


def _tracker() -> LimitsTracker:
    tracker = LimitsTracker(timezone="America/New_York", clock=Clock())
    tracker.note_sdk_event(event(RAW_EVENT).rate_limit_info)
    return tracker


def test_the_route_answers_with_no_bearer(monkeypatch) -> None:
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    response = _client(tracker=_tracker()).get(LIMITS_PATH)
    assert response.status_code == 200
    body = response.json()
    LimitsResponse.model_validate(body)
    assert set(body) == {"available", "reason", "observed_at", "five_hour", "seven_day"}
    assert body["available"] is True
    assert body["five_hour"]["left_pct"] == 69.0
    assert body["five_hour"]["resets_at"].endswith("-05:00")
    assert body["five_hour"]["reset"] is False
    assert TOKEN not in response.text


def test_the_route_makes_no_http_request(no_http: list[str]) -> None:
    client = _client(tracker=_tracker())
    client.get(LIMITS_PATH)
    client.get(LIMITS_PATH, params={"fresh": "1", "refresh": "true"})
    assert no_http == []


def test_the_route_is_404_when_limits_is_off() -> None:
    response = _client(enabled=False, tracker=_tracker()).get(LIMITS_PATH)
    assert response.status_code == 404


def test_the_route_is_404_with_no_tracker() -> None:
    assert _client(tracker=None).get(LIMITS_PATH).status_code == 404


def test_the_app_serves_the_route() -> None:
    from joshua_core.main import build_app

    assert LIMITS_PATH in build_app().openapi()["paths"]


async def test_build_limits_follows_enabled_and_loads_the_saved_snapshot() -> None:
    from engine_fakes import make_settings
    from joshua_core import limits
    from joshua_core.main import _build_limits
    from test_limits import FakeStore

    try:
        off = make_settings(mcp="\nlimits:\n  enabled: false\n")
        assert await _build_limits(off, FakeStore()) is None  # type: ignore[arg-type]
        assert limits.installed() is None

        saved = {
            "observed_at": "2026-11-08T01:00:00+00:00",
            "five_hour": {"used_pct": 12.0, "resets_at": None, "status": None},
        }
        tracker = await _build_limits(make_settings(), FakeStore(saved))  # type: ignore[arg-type]
        assert tracker is not None
        assert limits.installed() is tracker
        assert tracker.snapshot is not None
        assert tracker.snapshot.five_hour is not None
        assert tracker.snapshot.five_hour.used_pct == 12.0
    finally:
        limits.install(None)
