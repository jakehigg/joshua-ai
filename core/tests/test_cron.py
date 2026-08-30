"""Unit tests for the scheduler time math: cron and one-time phrases."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from joshua_core.engine.cron import compute_next_cron, parse_when

TZ = "America/New_York"
Z = ZoneInfo(TZ)


def _at(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=Z)


def test_daily_cron_finds_next_day() -> None:
    after = _at(2026, 8, 26, 10, 0)  # 10:00, past 09:00 today
    nxt = compute_next_cron("0 9 * * *", after, TZ)
    assert nxt == _at(2026, 8, 27, 9, 0)


def test_daily_cron_same_day_when_time_ahead() -> None:
    after = _at(2026, 8, 26, 7, 30)
    nxt = compute_next_cron("0 9 * * *", after, TZ)
    assert nxt == _at(2026, 8, 26, 9, 0)


def test_weekly_cron_day_of_week() -> None:
    # 18:00 on Thursday (cron dow 4). 2026-08-26 is a Wednesday.
    nxt = compute_next_cron("0 18 * * 4", _at(2026, 8, 26, 12, 0), TZ)
    assert nxt == _at(2026, 8, 27, 18, 0)
    assert nxt.weekday() == 3  # Python Thursday


def test_step_and_range_fields() -> None:
    nxt = compute_next_cron("*/15 9-17 * * *", _at(2026, 8, 26, 9, 7), TZ)
    assert nxt == _at(2026, 8, 26, 9, 15)


def test_next_is_strictly_after() -> None:
    exact = _at(2026, 8, 26, 9, 0)
    nxt = compute_next_cron("0 9 * * *", exact, TZ)
    assert nxt == _at(2026, 8, 27, 9, 0)


def test_dom_or_dow_matches_either() -> None:
    # Both day fields restricted: fires on the 1st OR any Monday.
    nxt = compute_next_cron("0 0 1 * 1", _at(2026, 8, 26, 12, 0), TZ)
    # Next Monday (2026-08-31) comes before the 1st (2026-09-01).
    assert nxt == _at(2026, 8, 31, 0, 0)


@pytest.mark.parametrize(
    "expr",
    ["0 9 * * ", "* * * *", "60 * * * *", "0 9 * * 8", "*/0 * * * *"],
)
def test_bad_cron_raises(expr: str) -> None:
    with pytest.raises(ValueError):
        compute_next_cron(expr, _at(2026, 8, 26, 12, 0), TZ)


def test_parse_when_relative_words() -> None:
    now = _at(2026, 8, 26, 12, 0)
    assert parse_when("in 30 minutes", TZ, now) == _at(2026, 8, 26, 12, 30)
    assert parse_when("in 2 hours", TZ, now) == _at(2026, 8, 26, 14, 0)
    assert parse_when("in 1 day", TZ, now) == _at(2026, 8, 27, 12, 0)


def test_parse_when_compact() -> None:
    now = _at(2026, 8, 26, 12, 0)
    assert parse_when("45m", TZ, now) == _at(2026, 8, 26, 12, 45)


def test_parse_when_iso() -> None:
    now = _at(2026, 8, 26, 12, 0)
    assert parse_when("2026-08-27 09:00", TZ, now) == _at(2026, 8, 27, 9, 0)


def test_parse_when_clock_rolls_to_tomorrow() -> None:
    now = _at(2026, 8, 26, 12, 0)
    # 09:00 already passed today, so it lands tomorrow.
    assert parse_when("9:00", TZ, now) == _at(2026, 8, 27, 9, 0)
    # A later time today stays today.
    assert parse_when("6:30pm", TZ, now) == _at(2026, 8, 26, 18, 30)


def test_parse_when_tomorrow_prefix() -> None:
    now = _at(2026, 8, 26, 12, 0)
    assert parse_when("tomorrow 07:30", TZ, now) == _at(2026, 8, 27, 7, 30)


def test_parse_when_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_when("whenever", TZ, _at(2026, 8, 26, 12, 0))
    with pytest.raises(ValueError):
        parse_when("   ", TZ, _at(2026, 8, 26, 12, 0))
