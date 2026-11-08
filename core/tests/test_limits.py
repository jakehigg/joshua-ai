"""The subscription limits tracker: the parse of the usage endpoint, the rules
of a read (max_age, the lock, 429, a timeout, no token), the SDK fill-in, and
the saved snapshot. A mock transport stands in for Anthropic."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from joshua_core import limits
from joshua_core.engine.usage import UsageMeter
from joshua_core.limits import (
    ERROR_BACKOFF_S,
    NO_TOKEN,
    NOT_YET,
    USAGE_BETA,
    USAGE_URL,
    LimitsTracker,
    dump_snapshot,
    load_snapshot,
    parse_event_windows,
    parse_usage,
    uses_subscription,
)

TOKEN = "sk-ant-oat01-secret-test-token"
TZ = "America/New_York"
T0 = datetime(2026, 11, 8, 1, 0, tzinfo=UTC)

BODY: dict[str, Any] = {
    "five_hour": {
        "utilization": 23.0,
        "resets_at": "2026-11-08T03:30:00.842595+00:00",
        "limit_dollars": None,
        "used_dollars": None,
        "remaining_dollars": None,
        "locked_reason": None,
    },
    "seven_day": {
        "utilization": 41.5,
        "resets_at": "2026-11-10T01:00:00.842614+00:00",
        "limit_dollars": None,
    },
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "extra_usage": {
        "is_enabled": True,
        "monthly_limit": 2500,
        "used_credits": 125.0,
        "utilization": None,
        "currency": "USD",
        "decimal_places": 2,
    },
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 23,
            "severity": "normal",
            "resets_at": "2026-11-08T03:30:00.842595+00:00",
            "scope": None,
            "is_active": True,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 41,
            "severity": "normal",
            "resets_at": "2026-11-10T01:00:00.842614+00:00",
            "scope": None,
            "is_active": True,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 13,
            "resets_at": "2026-11-10T01:00:00.842614+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
            "is_active": False,
        },
    ],
    "some_new_key": {"nested": [1, 2, 3]},
    "another_null": None,
}

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


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class Upstream:
    """A mock usage endpoint. ``responses`` is used in order; the last repeats."""

    def __init__(self, *responses: Any, delay: float = 0.0) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.delay = delay

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        item = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


class FakeStore:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data
        self.saves: list[dict[str, Any]] = []

    async def load_limits_snapshot(self) -> dict[str, Any] | None:
        return self.data

    async def save_limits_snapshot(self, data: dict[str, Any]) -> None:
        self.saves.append(data)
        self.data = data


def ok(body: Any = None) -> httpx.Response:
    return httpx.Response(200, json=BODY if body is None else body)


def tracker(
    upstream: Upstream,
    *,
    clock: Clock | None = None,
    token: str = TOKEN,
    store: Any = None,
    max_age_s: int = 300,
) -> LimitsTracker:
    return LimitsTracker(
        token=lambda: token,
        timezone=TZ,
        max_age_s=max_age_s,
        store=store,
        clock=clock or Clock(),
        transport=upstream.transport,
    )


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


# --- the parse ---------------------------------------------------------------


def test_a_200_body_gives_both_windows_the_scoped_week_and_the_extra_usage() -> None:
    snap = parse_usage(BODY, T0)
    assert snap is not None
    assert snap.source == "usage_api"
    assert snap.five_hour is not None and snap.five_hour.used_pct == 23.0
    assert snap.five_hour.status == "normal"
    assert snap.five_hour.resets_at == datetime(2026, 11, 8, 3, 30, 0, 842595, tzinfo=UTC)
    assert snap.seven_day is not None and snap.seven_day.used_pct == 41.5
    assert [(s.name, s.used_pct) for s in snap.scoped] == [("Fable", 13.0)]
    assert snap.extra_usage is not None
    # monthly_limit and used_credits are cents: decimal_places is 2.
    assert snap.extra_usage.limit == 25.0
    assert snap.extra_usage.used == 1.25
    assert snap.extra_usage.currency == "USD"
    assert snap.extra_usage.enabled is True


def test_extra_usage_with_no_decimal_places_gives_no_amount() -> None:
    body = copy.deepcopy(BODY)
    del body["extra_usage"]["decimal_places"]
    snap = parse_usage(body, T0)
    assert snap is not None and snap.extra_usage is not None
    assert snap.extra_usage.used is None and snap.extra_usage.limit is None


def test_a_seven_day_model_object_is_a_scoped_limit() -> None:
    body = copy.deepcopy(BODY)
    body["seven_day_opus"] = {"utilization": 60.0, "resets_at": None}
    snap = parse_usage(body, T0)
    assert snap is not None
    assert [(s.name, s.used_pct) for s in snap.scoped] == [("Fable", 13.0), ("Opus", 60.0)]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"five_hour": None, "seven_day": "text", "limits": "no"},
        {"five_hour": {"utilization": "23"}, "limits": [None, 3, {"kind": "weekly_scoped"}]},
        {"five_hour": {"utilization": True}, "extra_usage": {"is_enabled": "yes"}},
        {"five_hour": {"utilization": 5, "resets_at": "not a time"}},
        {"limits": [{"kind": "weekly_scoped", "percent": 9, "scope": {"surface": "web"}}]},
    ],
)
def test_missing_and_unknown_keys_never_fail(body: dict[str, Any]) -> None:
    snap = parse_usage(body, T0)
    assert snap is not None


def test_the_limits_list_fills_a_missing_window() -> None:
    body = {"limits": BODY["limits"]}
    snap = parse_usage(body, T0)
    assert snap is not None
    assert snap.five_hour is not None and snap.five_hour.used_pct == 23.0
    assert snap.seven_day is not None and snap.seven_day.used_pct == 41.0


def test_a_body_that_is_not_an_object_is_refused() -> None:
    assert parse_usage(["x"], T0) is None


def test_a_saved_snapshot_loads_back() -> None:
    snap = parse_usage(BODY, T0)
    assert snap is not None
    assert load_snapshot(dump_snapshot(snap)) == snap
    assert load_snapshot({"source": "other"}) is None
    assert load_snapshot("text") is None


# --- a read of the endpoint -----------------------------------------------------


async def test_a_read_sends_the_token_and_the_beta_header_and_answers() -> None:
    upstream = Upstream(ok())
    store = FakeStore()
    t = tracker(upstream, store=store)
    answer = await t.current()
    request = upstream.requests[0]
    assert str(request.url) == USAGE_URL
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["anthropic-beta"] == USAGE_BETA
    assert answer.available is True
    assert answer.source == "usage_api"
    assert answer.stale is False
    assert answer.retry_after is None
    assert answer.five_hour is not None
    assert answer.five_hour.left_pct == 77.0
    # 03:30 UTC is 22:30 the day before in New York.
    assert answer.five_hour.resets_at == "2026-11-07T22:30:00.842595-05:00"
    assert answer.observed_at == "2026-11-07T20:00:00-05:00"
    assert answer.scoped[0].left_pct == 87.0
    assert answer.extra_usage is not None and answer.extra_usage.limit == 25.0
    assert len(store.saves) == 1
    assert TOKEN not in str(answer.model_dump())
    assert TOKEN not in str(store.saves)


async def test_a_second_request_inside_max_age_makes_no_call() -> None:
    clock = Clock()
    upstream = Upstream(ok())
    t = tracker(upstream, clock=clock, max_age_s=300)
    await t.current()
    clock.advance(299)
    await t.current()
    assert len(upstream.requests) == 1
    clock.advance(1)
    await t.current()
    assert len(upstream.requests) == 2


async def test_two_concurrent_requests_make_one_call() -> None:
    upstream = Upstream(ok(), delay=0.05)
    t = tracker(upstream)
    first, second = await asyncio.gather(t.current(), t.current())
    assert len(upstream.requests) == 1
    assert first.available and second.available


async def test_a_429_keeps_the_snapshot_and_honours_retry_after(caplog) -> None:
    clock = Clock()
    limited = httpx.Response(
        429,
        json={"error": {"type": "rate_limit_error", "message": "Rate limited."}},
        headers={"retry-after": "3589"},
    )
    upstream = Upstream(ok(), limited, ok())
    t = tracker(upstream, clock=clock, max_age_s=300)
    await t.current()
    clock.advance(300)
    answer = await t.current()
    assert len(upstream.requests) == 2
    assert answer.available is True
    assert answer.stale is True
    assert answer.retry_after == 3589
    assert answer.five_hour is not None and answer.five_hour.used_pct == 23.0
    clock.advance(3000)
    answer = await t.current()
    assert len(upstream.requests) == 2
    assert answer.retry_after == 589
    clock.advance(589)
    answer = await t.current()
    assert len(upstream.requests) == 3
    assert answer.stale is False and answer.retry_after is None
    assert TOKEN not in caplog.text


async def test_a_429_with_no_retry_after_waits_the_back_off() -> None:
    clock = Clock()
    upstream = Upstream(httpx.Response(429), ok())
    t = tracker(upstream, clock=clock)
    answer = await t.current()
    assert answer.available is False and answer.reason == NOT_YET
    assert answer.retry_after == ERROR_BACKOFF_S
    clock.advance(ERROR_BACKOFF_S - 1)
    await t.current()
    assert len(upstream.requests) == 1


async def test_a_timeout_backs_off_for_a_minute() -> None:
    clock = Clock()
    upstream = Upstream(ok(), httpx.ReadTimeout("slow"), ok())
    t = tracker(upstream, clock=clock, max_age_s=60)
    await t.current()
    clock.advance(60)
    answer = await t.current()
    assert answer.stale is True and answer.retry_after == ERROR_BACKOFF_S
    assert answer.five_hour is not None
    clock.advance(30)
    await t.current()
    assert len(upstream.requests) == 2
    clock.advance(30)
    answer = await t.current()
    assert len(upstream.requests) == 3
    assert answer.stale is False


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json=[1, 2]),
        httpx.ConnectError("refused"),
    ],
)
async def test_another_failure_backs_off_too(response: Any) -> None:
    upstream = Upstream(response)
    t = tracker(upstream)
    answer = await t.current()
    assert answer.available is False
    assert answer.stale is True
    assert answer.retry_after == ERROR_BACKOFF_S
    await t.current()
    assert len(upstream.requests) == 1


async def test_no_token_makes_no_call_and_says_so() -> None:
    upstream = Upstream(ok())
    t = tracker(upstream, token="")
    answer = await t.current()
    assert answer.available is False
    assert answer.reason == NO_TOKEN
    assert upstream.requests == []


async def test_a_restart_keeps_the_saved_snapshot_and_its_age() -> None:
    clock = Clock()
    store = FakeStore()
    first = tracker(Upstream(ok()), clock=clock, store=store)
    await first.current()
    clock.advance(60)
    upstream = Upstream(ok())
    second = tracker(upstream, clock=clock, store=store)
    await second.load()
    answer = await second.current()
    assert answer.available is True and answer.five_hour is not None
    assert upstream.requests == []


async def test_a_store_that_fails_never_breaks_a_read() -> None:
    class Broken:
        async def load_limits_snapshot(self) -> None:
            raise RuntimeError("db down")

        async def save_limits_snapshot(self, data: dict[str, Any]) -> None:
            raise RuntimeError("db down")

    t = tracker(Upstream(ok()), store=Broken())
    await t.load()
    answer = await t.current()
    assert answer.available is True


# --- the SDK fill-in ------------------------------------------------------------


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


async def test_an_event_updates_the_windows_and_keeps_the_rest() -> None:
    clock = Clock()
    store = FakeStore()
    t = tracker(Upstream(ok()), clock=clock, store=store)
    await t.current()
    clock.advance(10)
    assert t.note_sdk_event(event(RAW_EVENT).rate_limit_info) is True
    await t.drain()
    answer = t.answer()
    assert answer.source == "sdk_event"
    assert answer.five_hour is not None and answer.five_hour.used_pct == 31.0
    assert answer.seven_day is not None and answer.seven_day.used_pct == 47.0
    assert [s.name for s in answer.scoped] == ["Fable"]
    assert answer.extra_usage is not None
    assert store.saves[-1]["source"] == "sdk_event"


async def test_an_event_fills_the_snapshot_while_the_endpoint_blocks() -> None:
    upstream = Upstream(httpx.Response(429, headers={"retry-after": "600"}))
    t = tracker(upstream)
    await t.current()
    t.note_sdk_event(event(RAW_EVENT).rate_limit_info)
    answer = await t.current()
    assert len(upstream.requests) == 1
    assert answer.available is True and answer.source == "sdk_event"
    assert answer.retry_after == 600


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
    t = tracker(Upstream(ok()))
    assert t.note_sdk_event(event(RAW_EVENT_CLI_2_1_294).rate_limit_info) is True
    answer = t.answer()
    assert answer.available is True and answer.source == "sdk_event"
    assert answer.five_hour is not None
    assert (answer.five_hour.used_pct, answer.five_hour.left_pct) == (61.0, 39.0)
    assert answer.five_hour.status is None
    assert answer.seven_day is not None and answer.seven_day.used_pct == 28.0


def test_an_event_from_cli_2_1_239_holds_no_percent_and_changes_nothing() -> None:
    t = tracker(Upstream(ok()))
    info = event(RAW_EVENT_CLI_2_1_239, rate_limit_type="five_hour").rate_limit_info
    assert t.note_sdk_event(info) is False
    assert t.snapshot is None


async def test_an_older_event_does_not_replace_a_newer_snapshot() -> None:
    clock = Clock()
    t = tracker(Upstream(ok()), clock=clock)
    await t.current()
    clock.advance(-5)
    assert t.note_sdk_event(event(RAW_EVENT).rate_limit_info) is False
    assert t.answer().source == "usage_api"


def test_note_event_reads_only_a_call_on_the_subscription() -> None:
    t = tracker(Upstream(ok()))
    limits.install(t)
    limits.note_event(event(RAW_EVENT), subscription=False)
    assert t.snapshot is None
    limits.note_event(SimpleNamespace(), subscription=True)
    assert t.snapshot is None
    limits.note_event(event(RAW_EVENT), subscription=True)
    assert t.snapshot is not None and t.snapshot.source == "sdk_event"


def test_note_event_with_no_tracker_or_a_bad_event_does_nothing() -> None:
    limits.note_event(event(RAW_EVENT), subscription=True)
    t = tracker(Upstream(ok()))
    limits.install(t)
    bad = type("RateLimitEvent", (), {"rate_limit_info": None})()
    limits.note_event(bad, subscription=True)
    assert t.snapshot is None


def test_the_usage_meter_gives_the_event_on_the_subscription_only() -> None:
    t = tracker(Upstream(ok()))
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
