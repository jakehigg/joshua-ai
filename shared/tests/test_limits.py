"""The windows of a RateLimitEvent, read with care."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from joshua_shared.limits import Window, epoch_time, number, parse_event_windows, pct, text


def info(raw: dict, **fields) -> SimpleNamespace:
    return SimpleNamespace(raw=raw, **fields)


def test_unified_windows_are_percent_with_the_reset_and_the_status() -> None:
    windows = parse_event_windows(
        info(
            {
                "unifiedWindows": {
                    "five_hour": {"utilization": 0.853, "resetsAt": 1794108600, "status": "ok"},
                    "seven_day": {"utilization": 0.4},
                }
            }
        )
    )
    assert windows["five_hour"] == Window(85.3, datetime.fromtimestamp(1794108600, UTC), "ok")
    assert windows["seven_day"] == Window(40.0, None, None)


def test_a_window_with_no_number_is_skipped_and_the_type_is_read() -> None:
    event = info(
        {"unifiedWindows": {"five_hour": {"utilization": "high"}, "seven_day": "x"}},
        rate_limit_type="five_hour",
        utilization=0.5,
        resets_at=None,
        status="allowed",
    )
    assert parse_event_windows(event) == {"five_hour": Window(50.0, None, "allowed")}


def test_an_unknown_type_or_no_info_gives_no_window() -> None:
    assert parse_event_windows(info({}, rate_limit_type="overage", utilization=0.5)) == {}
    assert parse_event_windows(None) == {}


def test_the_helpers_refuse_what_is_not_a_value() -> None:
    assert number(True) is None
    assert number(float("nan")) is None
    assert number(3) == 3.0
    assert text("  ") is None
    assert text(" a ") == "a"
    assert epoch_time(0) is None
    assert epoch_time(1e30) is None
    assert pct(12.345) == 12.3
