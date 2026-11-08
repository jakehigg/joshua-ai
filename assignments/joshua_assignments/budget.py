"""The budget of an assignment: the ceilings on the subscription windows.

Before each slice, the runner reads core's ``GET /v1/limits`` and calls
``check_limits``. During a slice, it reads each ``RateLimitEvent`` with
``joshua_shared.limits.parse_event_windows`` and calls ``check_windows``. A
window at or over its ceiling stops the assignment as ``paused_limits``.

A window that core cannot give is unknown. ``assignments.limits.when_unknown``
says what happens then: ``run`` runs, and ``refuse`` stops, unless the spec
has a ``budget.tokens`` cap, which limits the assignment on its own.

The texts here go to the agent of the assignment (``budget_line``) and to
Joshua (``limits_stop_text``). They come from numbers and the manager's own
words only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from joshua_shared.contracts import LimitsResponse
from joshua_shared.limits import WINDOWS, Window

LABELS = {"five_hour": "5-hour window", "seven_day": "weekly window"}
# The words in a checkpoint note that start the next step.
_NEXT_RE = re.compile(r"\bnext(?:\s+step)?\s*:\s*", re.IGNORECASE)
MAX_NEXT_STEP = 300
REASON_UNKNOWN = "limits_unknown"


@dataclass(frozen=True)
class Ceilings:
    """The ceilings of one assignment, in percent used."""

    five_hour_pct: int
    seven_day_pct: int

    @classmethod
    def from_spec(cls, spec: dict[str, Any]) -> Ceilings:
        budget = spec.get("budget") or {}
        return cls(int(budget["five_hour_pct"]), int(budget["seven_day_pct"]))

    def of(self, window: str) -> int:
        return self.five_hour_pct if window == "five_hour" else self.seven_day_pct


@dataclass(frozen=True)
class LimitsCheck:
    """What one read of the limits says about one assignment.

    ``over`` names the first window at or over its ceiling, or is None.
    ``unknown`` names the windows with no value. ``windows`` holds the
    percent used and the reset time of each known window, for the row.
    """

    windows: dict[str, dict[str, Any]] = field(default_factory=dict)
    unknown: tuple[str, ...] = ()
    over: str | None = None

    @property
    def is_unknown(self) -> bool:
        return bool(self.unknown)

    def used(self, window: str) -> float | None:
        item = self.windows.get(window)
        return float(item["used_pct"]) if item else None


def _over(windows: dict[str, dict[str, Any]], ceilings: Ceilings) -> str | None:
    for name in WINDOWS:
        item = windows.get(name)
        if item is not None and float(item["used_pct"]) >= ceilings.of(name):
            return name
    return None


def check_limits(answer: LimitsResponse | None, ceilings: Ceilings) -> LimitsCheck:
    """Check core's answer against the ceilings. None (no answer) is unknown."""
    windows: dict[str, dict[str, Any]] = {}
    if answer is not None and answer.available:
        for name in WINDOWS:
            window = getattr(answer, name)
            if window is not None:
                windows[name] = {"used_pct": window.used_pct, "resets_at": window.resets_at}
    unknown = tuple(name for name in WINDOWS if name not in windows)
    return LimitsCheck(windows=windows, unknown=unknown, over=_over(windows, ceilings))


def check_windows(windows: dict[str, Window], ceilings: Ceilings) -> LimitsCheck:
    """Check the windows of a ``RateLimitEvent``. A window the event does not give is unknown."""
    found = {
        name: {
            "used_pct": window.used_pct,
            "resets_at": window.resets_at.isoformat() if window.resets_at else None,
        }
        for name, window in windows.items()
        if name in WINDOWS
    }
    unknown = tuple(name for name in WINDOWS if name not in found)
    return LimitsCheck(windows=found, unknown=unknown, over=_over(found, ceilings))


def blocked_when_unknown(check: LimitsCheck, when_unknown: str, tokens: int | None) -> bool:
    """True when unknown limits must stop the assignment.

    ``when_unknown: refuse`` stops an assignment with no ``budget.tokens``
    cap while a window is unknown. ``run`` never stops for this.
    """
    return check.is_unknown and when_unknown == "refuse" and not tokens


def next_step(note: str | None) -> str | None:
    """The next step in a checkpoint note: the text after ``Next:``, else the whole note."""
    if not note or not note.strip():
        return None
    parts = _NEXT_RE.split(note, maxsplit=1)
    text = parts[1] if len(parts) == 2 and parts[1].strip() else note
    return " ".join(text.split())[:MAX_NEXT_STEP]


def stop_record(
    check: LimitsCheck,
    ceilings: Ceilings,
    *,
    at: datetime,
    source: str,
    next_step_text: str | None,
) -> dict[str, Any]:
    """The ``stopped_at_limits`` value of the row, for a stop at a ceiling or on unknown limits."""
    record: dict[str, Any] = {
        "at": at.isoformat(),
        "source": source,
        "windows": check.windows,
        "ceilings": {
            "five_hour_pct": ceilings.five_hour_pct,
            "seven_day_pct": ceilings.seven_day_pct,
        },
        "next_step": next_step_text,
    }
    if check.over is not None:
        record.update(
            window=check.over,
            used_pct=check.used(check.over),
            ceiling=ceilings.of(check.over),
            resets_at=check.windows[check.over].get("resets_at"),
        )
    else:
        record.update(reason=REASON_UNKNOWN, unknown=list(check.unknown))
    return record


def _when(resets_at: str | None, timezone: str) -> str | None:
    if not resets_at:
        return None
    try:
        moment = datetime.fromisoformat(resets_at)
    except ValueError:
        return None
    local = moment.astimezone(ZoneInfo(timezone))
    return f"{local:%H:%M} on {local:%Y-%m-%d}"


def _number(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def limits_stop_text(record: dict[str, Any], page: str | None, timezone: str) -> str:
    """The text of a limits stop, for the question event. It comes from the record only."""
    lines: list[str] = []
    window = record.get("window")
    if window in LABELS:
        lines.append(
            f"I stopped because the {LABELS[window]} is at {_number(record['used_pct'])} "
            f"percent, and my ceiling is {_number(record['ceiling'])} percent."
        )
        when = _when(record.get("resets_at"), timezone)
        lines.append(f"It resets at {when}." if when else "Core does not say when it resets.")
    else:
        lines.append(
            "I stopped because core cannot say how much of the subscription limits is "
            "used, and I have no token cap."
        )
    step = record.get("next_step")
    if step:
        lines.append(f"My next step: {step}.".replace("..", "."))
    lines.append(f"My plan and progress are at {page}.")
    if window in LABELS:
        lines.append("Tell me to continue, or stop me.")
    else:
        lines.append(
            "Tell me to continue when core knows the limits again, give me a token cap "
            "with extend_assignment, or stop me."
        )
    return " ".join(lines)


def duration_text(seconds: float) -> str:
    """``seconds`` as ``2h 05m``, ``12m``, or ``40s``."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def budget_line(
    check: LimitsCheck,
    ceilings: Ceilings,
    *,
    turns_left: int,
    working_left_s: float,
    tokens_left: int | None = None,
) -> str:
    """The budget line of a later prompt.

    ``Budget: 5-hour window at 40% of your 80% ceiling, weekly at 20% of 90%,
    12 turns left, 3h 10m of working time left.``
    """
    five = check.used("five_hour")
    seven = check.used("seven_day")
    pieces = [
        f"5-hour window at {_number(five)}% of your {ceilings.five_hour_pct}% ceiling"
        if five is not None
        else f"5-hour window unknown, your ceiling {ceilings.five_hour_pct}%",
        f"weekly at {_number(seven)}% of {ceilings.seven_day_pct}%"
        if seven is not None
        else f"weekly unknown, ceiling {ceilings.seven_day_pct}%",
        f"{max(0, turns_left)} turns left",
        f"{duration_text(working_left_s)} of working time left",
    ]
    if tokens_left is not None:
        pieces.append(f"{max(0, tokens_left)} tokens left")
    return "Budget: " + ", ".join(pieces) + "."
