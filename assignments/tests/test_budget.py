"""The ceilings, the unknown limits, the stop text, and the budget line."""

from __future__ import annotations

from datetime import UTC, datetime

from joshua_assignments import budget
from joshua_shared.contracts import LimitsResponse, LimitsWindow
from joshua_shared.limits import Window

CEILINGS = budget.Ceilings(80, 90)
NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


def answer(five: float | None, seven: float | None) -> LimitsResponse:
    def w(used):
        if used is None:
            return None
        return LimitsWindow(
            used_pct=used, left_pct=100 - used, resets_at="2026-03-04T23:30:00-05:00"
        )

    return LimitsResponse(available=True, five_hour=w(five), seven_day=w(seven))


def test_a_window_at_the_ceiling_is_over() -> None:
    assert budget.check_limits(answer(80, 10), CEILINGS).over == "five_hour"
    assert budget.check_limits(answer(79.9, 90), CEILINGS).over == "seven_day"
    check = budget.check_limits(answer(10, 10), CEILINGS)
    assert check.over is None and not check.is_unknown


def test_no_answer_or_no_window_is_unknown() -> None:
    assert budget.check_limits(None, CEILINGS).unknown == ("five_hour", "seven_day")
    assert budget.check_limits(LimitsResponse(available=False), CEILINGS).is_unknown
    assert budget.check_limits(answer(10, None), CEILINGS).unknown == ("seven_day",)


def test_when_unknown_refuse_blocks_only_without_a_token_cap() -> None:
    unknown = budget.check_limits(None, CEILINGS)
    known = budget.check_limits(answer(1, 1), CEILINGS)
    assert budget.blocked_when_unknown(unknown, "refuse", None) is True
    assert budget.blocked_when_unknown(unknown, "refuse", 1000) is False
    assert budget.blocked_when_unknown(unknown, "run", None) is False
    assert budget.blocked_when_unknown(known, "refuse", None) is False


def test_the_windows_of_an_event() -> None:
    check = budget.check_windows({"five_hour": Window(85.0, NOW)}, CEILINGS)
    assert check.over == "five_hour" and check.unknown == ("seven_day",)
    assert check.windows["five_hour"]["resets_at"] == NOW.isoformat()
    assert budget.check_windows({"seven_day": Window(5.0)}, CEILINGS).over is None


def test_next_step() -> None:
    assert budget.next_step("plan written. Next: clone the repo") == "clone the repo"
    assert budget.next_step("done 2. next step:  file\nissues") == "file issues"
    assert budget.next_step("all fine") == "all fine"
    assert budget.next_step("Next:") == "Next:"
    assert budget.next_step("  ") is None and budget.next_step(None) is None


def test_the_stop_text_in_the_instance_timezone() -> None:
    check = budget.check_limits(answer(80, 10), CEILINGS)
    record = budget.stop_record(check, CEILINGS, at=NOW, source="limits", next_step_text="step 3.")
    text = budget.limits_stop_text(record, "assignments/1-x.md", "America/New_York")
    assert text == (
        "I stopped because the 5-hour window is at 80 percent, and my ceiling is 80 percent. "
        "It resets at 23:30 on 2026-03-04. My next step: step 3. "
        "My plan and progress are at assignments/1-x.md. Tell me to continue, or stop me."
    )
    record["resets_at"] = None
    record["used_pct"] = 80.5
    assert "80.5 percent" in budget.limits_stop_text(record, "p", "UTC")
    assert "Core does not say when it resets." in budget.limits_stop_text(record, "p", "UTC")
    record["resets_at"] = "soon"
    assert "Core does not say" in budget.limits_stop_text(record, "p", "UTC")


def test_the_unknown_stop_text() -> None:
    check = budget.check_limits(None, CEILINGS)
    record = budget.stop_record(check, CEILINGS, at=NOW, source="limits", next_step_text=None)
    assert record["reason"] == "limits_unknown"
    text = budget.limits_stop_text(record, "p", "UTC")
    assert "cannot say how much" in text and "extend_assignment" in text


def test_the_budget_line() -> None:
    line = budget.budget_line(
        budget.check_limits(answer(40, 20.5), CEILINGS),
        CEILINGS,
        turns_left=12,
        working_left_s=3 * 3600 + 600,
        tokens_left=500,
    )
    assert line == (
        "Budget: 5-hour window at 40% of your 80% ceiling, weekly at 20.5% of 90%, "
        "12 turns left, 3h 10m of working time left, 500 tokens left."
    )
    unknown = budget.budget_line(
        budget.check_limits(None, CEILINGS), CEILINGS, turns_left=-1, working_left_s=59
    )
    assert "5-hour window unknown, your ceiling 80%" in unknown
    assert "weekly unknown, ceiling 90%" in unknown and "0 turns left" in unknown
    assert "59s of working time" in unknown
    assert budget.duration_text(120) == "2m"
