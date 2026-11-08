"""The subscription limits tracker: the parse of the ``RateLimitEvent`` of a
model call, the answer with its ``reset`` flags, the saved snapshot, and the
proof that core makes no HTTP request for the limits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from joshua_core import limits
from joshua_core.engine.usage import UsageMeter
from joshua_core.limits import (
    NOT_YET,
    LimitsTracker,
    Snapshot,
    Window,
    dump_snapshot,
    load_snapshot,
    parse_event_windows,
    uses_subscription,
)

TOKEN = "sk-ant-oat01-secret-test-token"
TZ = "America/New_York"
T0 = datetime(2026, 11, 8, 1, 0, tzinfo=UTC)

RAW_EVENT = {
    "status": "allowed",
    "resetsAt": 1794108600,
    "rateLimitType": "five_hour",
    "overageStatus": "allowed",
    "overageResetsAt": 1796083200,
    "isUsingOverage": False,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.31, "resetsAt": 1794108600, "status": "allowed"},
        "seven_day": {"utilization": 0.47, "resetsAt": 1794272400, "status": "allowed_warning"},
    },
}
# The reset times of RAW_EVENT.
FIVE_HOUR_RESET = datetime(2026, 11, 8, 3, 30, tzinfo=UTC)
SEVEN_DAY_RESET = datetime(2026, 11, 10, 1, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class FakeStore:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data
        self.saves: list[dict[str, Any]] = []

    async def load_limits_snapshot(self) -> dict[str, Any] | None:
        return self.data

    async def save_limits_snapshot(self, data: dict[str, Any]) -> None:
        self.saves.append(data)
        self.data = data


def tracker(*, clock: Clock | None = None, store: Any = None) -> LimitsTracker:
    return LimitsTracker(timezone=TZ, store=store, clock=clock or Clock())


def event(raw: dict[str, Any] | None = None, **fields: Any) -> Any:
    info = SimpleNamespace(
        status=fields.get("status", "allowed"),
        resets_at=fields.get("resets_at"),
        rate_limit_type=fields.get("rate_limit_type"),
        utilization=fields.get("utilization"),
        raw=raw if raw is not None else {},
    )
    return type("RateLimitEvent", (), {"rate_limit_info": info})()


@pytest.fixture(autouse=True)
def _no_installed_tracker():
    limits.install(None)
    yield
    limits.install(None)


# --- the answer ------------------------------------------------------------------


def test_with_no_event_the_answer_says_why() -> None:
    answer = tracker().answer()
    assert answer.available is False
    assert answer.reason == NOT_YET
    assert answer.five_hour is None and answer.seven_day is None


async def test_an_event_gives_both_windows_in_the_instance_timezone() -> None:
    store = FakeStore()
    t = tracker(store=store)
    assert t.note_sdk_event(event(RAW_EVENT).rate_limit_info) is True
    await t.drain()
    answer = t.answer()
    assert answer.available is True
    assert answer.observed_at == "2026-11-07T20:00:00-05:00"
    assert answer.five_hour is not None
    assert (answer.five_hour.used_pct, answer.five_hour.left_pct) == (31.0, 69.0)
    # 03:30 UTC is 22:30 the day before in New York.
    assert answer.five_hour.resets_at == "2026-11-07T22:30:00-05:00"
    assert answer.five_hour.reset is False
    assert answer.seven_day is not None and answer.seven_day.status == "allowed_warning"
    assert answer.seven_day.reset is False
    assert set(store.saves[-1]) == {"observed_at", "five_hour", "seven_day"}
    assert TOKEN not in str(answer.model_dump())


def test_a_window_whose_reset_time_passed_is_marked_reset() -> None:
    clock = Clock()
    t = tracker(clock=clock)
    t.note_sdk_event(event(RAW_EVENT).rate_limit_info)
    clock.now = FIVE_HOUR_RESET - timedelta(seconds=1)
    answer = t.answer()
    assert answer.five_hour is not None and answer.five_hour.reset is False
    clock.now = FIVE_HOUR_RESET
    answer = t.answer()
    assert answer.five_hour is not None and answer.five_hour.reset is True
    # The numbers stay the last ones until the next call.
    assert answer.five_hour.used_pct == 31.0
    assert answer.seven_day is not None and answer.seven_day.reset is False
    clock.now = SEVEN_DAY_RESET + timedelta(days=1)
    answer = t.answer()
    assert answer.seven_day is not None and answer.seven_day.reset is True
    # The observed time does not move: no call came.
    assert answer.observed_at == "2026-11-07T20:00:00-05:00"


def test_a_new_event_after_the_reset_clears_the_flag() -> None:
    clock = Clock(FIVE_HOUR_RESET + timedelta(minutes=5))
    t = tracker(clock=clock)
    t._snapshot = Snapshot(T0, five_hour=Window(80.0, FIVE_HOUR_RESET))
    answer = t.answer()
    assert answer.five_hour is not None and answer.five_hour.reset is True
    raw = {"unifiedWindows": {"five_hour": {"utilization": 0.02, "resetsAt": 1794126600}}}
    assert t.note_sdk_event(event(raw).rate_limit_info) is True
    answer = t.answer()
    assert answer.five_hour is not None
    assert answer.five_hour.reset is False and answer.five_hour.used_pct == 2.0


def test_a_window_with_no_reset_time_is_never_reset() -> None:
    clock = Clock()
    t = tracker(clock=clock)
    t.note_sdk_event(event({}, rate_limit_type="seven_day", utilization=0.5).rate_limit_info)
    clock.advance(10 * 365 * 86400)
    answer = t.answer()
    assert answer.seven_day is not None and answer.seven_day.reset is False


# --- the saved snapshot ---------------------------------------------------------


def test_a_saved_snapshot_loads_back() -> None:
    snap = Snapshot(T0, five_hour=Window(31.0, FIVE_HOUR_RESET, "allowed"))
    assert load_snapshot(dump_snapshot(snap)) == snap
    assert load_snapshot({"five_hour": {"used_pct": 1}}) is None
    assert load_snapshot("text") is None


def test_an_old_saved_snapshot_with_more_keys_still_loads() -> None:
    old = {
        "source": "usage_api",
        "observed_at": "2026-11-08T01:00:00+00:00",
        "api_observed_at": "2026-11-08T01:00:00+00:00",
        "five_hour": {"used_pct": 23.0, "resets_at": "2026-11-08T03:30:00+00:00", "status": None},
        "seven_day": {"used_pct": 41.5, "resets_at": None, "status": "normal"},
        "scoped": [{"name": "Fable", "used_pct": 13.0, "resets_at": None}],
        "extra_usage": {"enabled": True, "used": 1.25, "limit": 25.0, "currency": "USD"},
    }
    snap = load_snapshot(old)
    assert snap == Snapshot(
        T0, five_hour=Window(23.0, FIVE_HOUR_RESET), seven_day=Window(41.5, None, "normal")
    )


async def test_a_restart_keeps_the_saved_snapshot() -> None:
    clock = Clock()
    store = FakeStore()
    first = tracker(clock=clock, store=store)
    first.note_sdk_event(event(RAW_EVENT).rate_limit_info)
    await first.drain()
    clock.advance(60)
    second = tracker(clock=clock, store=store)
    await second.load()
    answer = second.answer()
    assert answer.available is True and answer.five_hour is not None
    assert answer.five_hour.used_pct == 31.0


async def test_a_store_that_fails_never_breaks_an_event() -> None:
    class Broken:
        async def load_limits_snapshot(self) -> None:
            raise RuntimeError("db down")

        async def save_limits_snapshot(self, data: dict[str, Any]) -> None:
            raise RuntimeError("db down")

    t = tracker(store=Broken())
    await t.load()
    assert t.note_sdk_event(event(RAW_EVENT).rate_limit_info) is True
    await t.drain()
    assert t.answer().available is True


async def test_an_older_event_does_not_replace_a_newer_snapshot() -> None:
    clock = Clock()
    t = tracker(clock=clock)
    t._snapshot = Snapshot(T0 + timedelta(seconds=5), five_hour=Window(50.0))
    assert t.note_sdk_event(event(RAW_EVENT).rate_limit_info) is False
    assert t.snapshot is not None and t.snapshot.five_hour == Window(50.0)


# --- no HTTP ------------------------------------------------------------------------


async def test_core_makes_no_http_request_for_the_limits(no_http: list[str]) -> None:
    store = FakeStore()
    t = LimitsTracker(timezone=TZ, store=store)
    await t.load()
    t.answer()
    t.note_sdk_event(event(RAW_EVENT).rate_limit_info)
    await t.drain()
    t.answer()
    assert no_http == []
    assert not hasattr(limits, "httpx")


# --- the events ------------------------------------------------------------------


def test_unified_windows_give_both_windows_as_percent() -> None:
    windows = parse_event_windows(event(RAW_EVENT).rate_limit_info)
    assert windows["five_hour"].used_pct == 31.0
    assert windows["five_hour"].resets_at == datetime.fromtimestamp(1794108600, UTC)
    assert windows["seven_day"].used_pct == 47.0
    assert windows["seven_day"].status == "allowed_warning"


def test_with_no_unified_windows_the_type_and_utilization_are_read() -> None:
    info = event(
        {"status": "allowed"}, rate_limit_type="seven_day", utilization=0.5
    ).rate_limit_info
    windows = parse_event_windows(info)
    assert list(windows) == ["seven_day"]
    assert windows["seven_day"].used_pct == 50.0
    assert (
        parse_event_windows(event({}, rate_limit_type="overage", utilization=0.5).rate_limit_info)
        == {}
    )
    assert parse_event_windows(event({}, rate_limit_type="five_hour").rate_limit_info) == {}


# The rate_limit_info of each bundled CLI, as captured on 2026-11-08 with a
# subscription token. CLI 2.1.239 (claude-agent-sdk 0.2.144) sends no
# percentages, so core needs claude-agent-sdk >= 0.2.165 (CLI 2.1.294), which
# sends unifiedWindows without a status for each window.
RAW_EVENT_CLI_2_1_239 = {
    "status": "allowed",
    "resetsAt": 1794108600,
    "rateLimitType": "five_hour",
    "overageStatus": "allowed",
    "overageResetsAt": 1796083200,
    "isUsingOverage": False,
}
RAW_EVENT_CLI_2_1_294 = {
    **RAW_EVENT_CLI_2_1_239,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.61, "resetsAt": 1794108600},
        "seven_day": {"utilization": 0.28, "resetsAt": 1794272400},
    },
}


def test_an_event_from_cli_2_1_294_fills_both_windows() -> None:
    t = tracker()
    assert t.note_sdk_event(event(RAW_EVENT_CLI_2_1_294).rate_limit_info) is True
    answer = t.answer()
    assert answer.available is True
    assert answer.five_hour is not None
    assert (answer.five_hour.used_pct, answer.five_hour.left_pct) == (61.0, 39.0)
    assert answer.five_hour.status is None
    assert answer.seven_day is not None and answer.seven_day.used_pct == 28.0


def test_an_event_from_cli_2_1_239_holds_no_percent_and_changes_nothing() -> None:
    t = tracker()
    info = event(RAW_EVENT_CLI_2_1_239, rate_limit_type="five_hour").rate_limit_info
    assert t.note_sdk_event(info) is False
    assert t.snapshot is None


def test_note_event_reads_only_a_call_on_the_subscription() -> None:
    t = tracker()
    limits.install(t)
    limits.note_event(event(RAW_EVENT), subscription=False)
    assert t.snapshot is None
    limits.note_event(SimpleNamespace(), subscription=True)
    assert t.snapshot is None
    limits.note_event(event(RAW_EVENT), subscription=True)
    assert t.snapshot is not None


def test_note_event_with_no_tracker_or_a_bad_event_does_nothing() -> None:
    limits.note_event(event(RAW_EVENT), subscription=True)
    t = tracker()
    limits.install(t)
    bad = type("RateLimitEvent", (), {"rate_limit_info": None})()
    limits.note_event(bad, subscription=True)
    assert t.snapshot is None


def test_the_usage_meter_gives_the_event_on_the_subscription_only() -> None:
    t = tracker()
    limits.install(t)
    other = UsageMeter(subscription=False)
    other.note(event(RAW_EVENT))
    assert t.snapshot is None
    assert other.seen is False
    meter = UsageMeter(subscription=True)
    meter.note(event(RAW_EVENT))
    assert t.snapshot is not None
    assert meter.seen is False


def test_uses_subscription_reads_the_oauth_variable() -> None:
    assert uses_subscription({"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}) is True
    assert uses_subscription({"CLAUDE_CODE_OAUTH_TOKEN": "", "ANTHROPIC_AUTH_TOKEN": "k"}) is False
    assert uses_subscription(None) is False
