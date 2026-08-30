"""Time math for the scheduler: cron expressions and one-time ``when`` phrases.

``compute_next_cron`` returns the next fire time strictly after a reference
instant for a standard five-field cron expression (``minute hour day-of-month
month day-of-week``). ``parse_when`` turns a one-time phrase — relative
(``in 30 minutes``) or absolute (``2026-08-27 09:00``, ``9:00am``) — into a
timezone-aware datetime. Both work in the configured timezone.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# minute, hour, day-of-month, month, day-of-week; (min, max) inclusive per field.
_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
_CRON_FIELDS = len(_FIELD_RANGES)
# A valid cron fires well within this window; the scan stops here so a bad
# expression raises instead of looping forever.
_MAX_SCAN_DAYS = 1500

_REL_RE = re.compile(r"^in\s+(\d+)\s*(second|sec|minute|min|hour|hr|day|week)s?$")
_COMPACT_RE = re.compile(r"^(\d+)\s*(s|m|h|d|w)$")
_CLOCK_RE = re.compile(r"^(?:(today|tomorrow)\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$")

_UNIT_SECONDS = {
    "second": 1,
    "sec": 1,
    "s": 1,
    "minute": 60,
    "min": 60,
    "m": 60,
    "hour": 3600,
    "hr": 3600,
    "h": 3600,
    "day": 86400,
    "d": 86400,
    "week": 604800,
    "w": 604800,
}


def zone(tz: str | ZoneInfo) -> ZoneInfo:
    """Return a ``ZoneInfo`` for a name or an already-built zone."""
    return tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)


def now_in(tz: str | ZoneInfo) -> datetime:
    """The current time in the configured timezone."""
    return datetime.now(zone(tz))


def _localize(dt: datetime, z: ZoneInfo) -> datetime:
    """Attach ``z`` to a naive datetime, else convert into ``z``."""
    return dt.replace(tzinfo=z) if dt.tzinfo is None else dt.astimezone(z)


# --- cron ------------------------------------------------------------------


def _parse_field(field: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        base = part
        if "/" in part:
            base, step_text = part.split("/", 1)
            step = int(step_text)
            if step <= 0:
                raise ValueError(f"cron step must be positive: {part!r}")
        if base in ("*", ""):
            start, end = lo, hi
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(base)
        if start < lo or end > hi or start > end:
            raise ValueError(f"cron field out of range: {part!r}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"empty cron field: {field!r}")
    return values


def parse_cron(expr: str) -> tuple[tuple[set[int], ...], bool, bool]:
    """Parse a cron expression into per-field value sets.

    Returns ``(sets, dom_is_star, dow_is_star)``. The two flags carry whether the
    day-of-month and day-of-week fields are ``*``, which selects the standard
    "match on either restricted day field" behavior.
    """
    fields = expr.split()
    if len(fields) != _CRON_FIELDS:
        raise ValueError(f"cron needs {_CRON_FIELDS} fields, got {len(fields)}: {expr!r}")
    sets = tuple(_parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, _FIELD_RANGES, strict=True))
    return sets, fields[2].strip() == "*", fields[4].strip() == "*"


def _day_ok(dt: datetime, doms: set[int], dows: set[int], dom_star: bool, dow_star: bool) -> bool:
    # cron day-of-week: Sunday=0..Saturday=6; Python weekday(): Monday=0..Sunday=6.
    cron_dow = (dt.weekday() + 1) % 7
    dom_match = dt.day in doms
    dow_match = cron_dow in dows
    if dom_star and dow_star:
        return True
    if dom_star:
        return dow_match
    if dow_star:
        return dom_match
    return dom_match or dow_match  # both restricted: either one fires


def compute_next_cron(expr: str, after: datetime, tz: str | ZoneInfo) -> datetime:
    """The first fire time strictly after ``after`` for cron ``expr``, in ``tz``.

    Raises ``ValueError`` for a malformed expression or one that never fires
    within the scan window.
    """
    z = zone(tz)
    (minutes, hours, doms, months, dows), dom_star, dow_star = parse_cron(expr)
    start = _localize(after, z)
    candidate = (start + timedelta(minutes=1)).replace(second=0, microsecond=0)
    limit = candidate + timedelta(days=_MAX_SCAN_DAYS)
    while candidate <= limit:
        if candidate.month in months and _day_ok(candidate, doms, dows, dom_star, dow_star):
            if candidate.hour in hours and candidate.minute in minutes:
                return candidate
            candidate += timedelta(minutes=1)
        else:
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
    raise ValueError(f"cron expression never fires within {_MAX_SCAN_DAYS} days: {expr!r}")


# --- one-time phrases ------------------------------------------------------


def parse_when(when: str, tz: str | ZoneInfo, now: datetime | None = None) -> datetime:
    """Turn a one-time phrase into a timezone-aware datetime in ``tz``.

    Understands relative offsets (``in 30 minutes``, ``2h``), ISO 8601 timestamps,
    and clock times (``9:00``, ``9:00am``, ``tomorrow 07:30``). A bare clock time
    lands today when still ahead, else tomorrow. Raises ``ValueError`` when the
    phrase is empty or not understood.
    """
    z = zone(tz)
    ref = _localize(now, z) if now is not None else datetime.now(z)
    text = when.strip().lower()
    if not text:
        raise ValueError("no time given")

    match = _REL_RE.match(text) or _COMPACT_RE.match(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        return ref + timedelta(seconds=amount * _UNIT_SECONDS[unit])

    iso = _try_iso(when.strip(), z)
    if iso is not None:
        return iso

    clock = _CLOCK_RE.match(text)
    if clock:
        return _from_clock(clock, ref)

    raise ValueError(f"could not read a time from {when!r}")


def _try_iso(text: str, z: ZoneInfo) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _localize(parsed, z)


def _from_clock(match: re.Match[str], ref: datetime) -> datetime:
    day_word, hour_text, minute_text, meridiem = match.groups()
    hour = int(hour_text)
    minute = int(minute_text) if minute_text else 0
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError("clock time out of range")
    target = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day_word == "tomorrow":
        target += timedelta(days=1)
    elif day_word != "today" and target <= ref:
        target += timedelta(days=1)  # a bare past time means the next day
    return target
