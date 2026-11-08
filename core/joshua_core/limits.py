"""How much of the Claude subscription limits is used: the 5-hour window and
the weekly window.

Each model call on the subscription sends a ``RateLimitEvent``. ``note_event``
reads it with ``joshua_shared.limits.parse_event_windows``, which this module
also exports, and updates the 5-hour and the weekly window, at no cost. A call on
another server sends no limits, and core does not read its events. Core makes
no HTTP request for the limits.

The numbers are known after the first model call on the subscription. The
last snapshot is kept in the ``limits_snapshot`` table, so a restart keeps it.
When the reset time of a window is in the past, the answer marks the window
``reset``: its numbers are old until the next model call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from joshua_shared.contracts import LimitsResponse, LimitsWindow
from joshua_shared.limits import (
    Window,
    number,
    parse_event_windows,
    pct,
    text,
)
from joshua_shared.log import get_logger

logger = get_logger("limits")

NOT_YET = "no model call on the subscription has given the limits yet"

# The variable that holds the subscription token in the CLI environment of a call.
_OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def uses_subscription(env: Mapping[str, str] | None) -> bool:
    """True when the CLI environment ``env`` of a call holds the subscription
    token, so the call runs on the subscription."""
    return bool(env and env.get(_OAUTH_ENV))


# ── the snapshot ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Snapshot:
    observed_at: datetime
    five_hour: Window | None = None
    seven_day: Window | None = None


_number = number
_text = text
_pct = pct


def _iso_time(value: Any) -> datetime | None:
    """An ISO 8601 time with an offset, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# ── keeping the snapshot ────────────────────────────────────────────────────


def _dump_time(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _dump_window(window: Window | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {
        "used_pct": window.used_pct,
        "resets_at": _dump_time(window.resets_at),
        "status": window.status,
    }


def dump_snapshot(snapshot: Snapshot) -> dict[str, Any]:
    """The snapshot as JSON data, with UTC times, for the database."""
    return {
        "observed_at": _dump_time(snapshot.observed_at),
        "five_hour": _dump_window(snapshot.five_hour),
        "seven_day": _dump_window(snapshot.seven_day),
    }


def _load_window(value: Any) -> Window | None:
    if not isinstance(value, Mapping):
        return None
    used = _number(value.get("used_pct"))
    if used is None:
        return None
    return Window(used, _iso_time(value.get("resets_at")), _text(value.get("status")))


def load_snapshot(data: Any) -> Snapshot | None:
    """A snapshot from ``dump_snapshot`` data, or None for data it cannot read.
    It ignores a key that it does not know."""
    if not isinstance(data, Mapping):
        return None
    observed_at = _iso_time(data.get("observed_at"))
    if observed_at is None:
        return None
    return Snapshot(
        observed_at=observed_at,
        five_hour=_load_window(data.get("five_hour")),
        seven_day=_load_window(data.get("seven_day")),
    )


class LimitsStore(Protocol):
    """The two ``Repo`` methods that keep the last snapshot."""

    async def load_limits_snapshot(self) -> dict[str, Any] | None: ...

    async def save_limits_snapshot(self, data: dict[str, Any]) -> None: ...


class LimitsTracker:
    """The snapshot that the rate-limit events of the model calls fill."""

    def __init__(
        self,
        *,
        timezone: str,
        store: LimitsStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._zone = ZoneInfo(timezone)
        self._store = store
        self._clock = clock or _utc_now
        self._snapshot: Snapshot | None = None
        self._saves: set[asyncio.Task[None]] = set()

    @property
    def snapshot(self) -> Snapshot | None:
        return self._snapshot

    # --- the database --------------------------------------------------------

    async def load(self) -> None:
        """Read the last snapshot from the store. A failure is a warning."""
        if self._store is None:
            return
        try:
            data = await self._store.load_limits_snapshot()
        except Exception as exc:  # noqa: BLE001 — the limits never stop a start
            logger.warning({"message": "limits snapshot not loaded", "error": type(exc).__name__})
            return
        loaded = load_snapshot(data)
        if loaded is not None and (
            self._snapshot is None or loaded.observed_at > self._snapshot.observed_at
        ):
            self._snapshot = loaded

    async def _save(self, snapshot: Snapshot) -> None:
        if self._store is None:
            return
        try:
            await self._store.save_limits_snapshot(dump_snapshot(snapshot))
        except Exception as exc:  # noqa: BLE001 — a lost save costs a restart only
            logger.warning({"message": "limits snapshot not saved", "error": type(exc).__name__})

    def _save_later(self, snapshot: Snapshot) -> None:
        """Save from code that cannot wait, such as ``note_sdk_event``."""
        if self._store is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._save(snapshot))
        self._saves.add(task)
        task.add_done_callback(self._saves.discard)

    async def drain(self) -> None:
        """Wait for the saves in progress."""
        if self._saves:
            await asyncio.gather(*list(self._saves), return_exceptions=True)

    # --- the events ------------------------------------------------------------

    def note_sdk_event(self, info: Any) -> bool:
        """Update the 5-hour and the weekly window from the ``rate_limit_info``
        of a ``RateLimitEvent``. Returns True when the snapshot changed."""
        windows = parse_event_windows(info)
        if not windows:
            return False
        now = self._clock()
        current = self._snapshot
        if current is not None and current.observed_at > now:
            return False
        if current is None:
            current = Snapshot(observed_at=now)
        updated = replace(
            current,
            observed_at=now,
            five_hour=windows.get("five_hour", current.five_hour),
            seven_day=windows.get("seven_day", current.seven_day),
        )
        self._snapshot = updated
        self._save_later(updated)
        return True

    # --- the answer ------------------------------------------------------------

    def _local(self, value: datetime | None) -> str | None:
        return value.astimezone(self._zone).isoformat() if value is not None else None

    def _window(self, window: Window | None, now: datetime) -> LimitsWindow | None:
        if window is None:
            return None
        return LimitsWindow(
            used_pct=window.used_pct,
            left_pct=_pct(max(0.0, 100.0 - window.used_pct)),
            resets_at=self._local(window.resets_at),
            status=window.status,
            reset=window.resets_at is not None and window.resets_at <= now,
        )

    def answer(self) -> LimitsResponse:
        """The answer of ``GET /v1/limits`` from what core knows now. No I/O."""
        snap = self._snapshot
        if snap is None:
            return LimitsResponse(available=False, reason=NOT_YET)
        now = self._clock()
        return LimitsResponse(
            available=True,
            observed_at=self._local(snap.observed_at),
            five_hour=self._window(snap.five_hour, now),
            seven_day=self._window(snap.seven_day, now),
        )


# ── the tracker of this process ─────────────────────────────────────────────

_tracker: LimitsTracker | None = None


def install(tracker: LimitsTracker | None) -> None:
    """Make ``tracker`` the one that ``note_event`` feeds. None stops it."""
    global _tracker
    _tracker = tracker


def installed() -> LimitsTracker | None:
    return _tracker


def note_event(msg: Any, *, subscription: bool) -> None:
    """Give a ``RateLimitEvent`` of a call on the subscription to the tracker.

    Any other message, a call on another server, or no tracker does nothing.
    It never raises, so it can never break a turn.
    """
    if not subscription or _tracker is None or type(msg).__name__ != "RateLimitEvent":
        return
    try:
        _tracker.note_sdk_event(getattr(msg, "rate_limit_info", None))
    except Exception as exc:  # noqa: BLE001 — the limits never break a turn
        logger.warning({"message": "rate limit event not read", "error": type(exc).__name__})
