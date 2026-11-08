"""How much of the Claude subscription limits is used: the 5-hour window and
the weekly window.

Two sources fill one snapshot:

- ``usage_api``: core reads ``GET /api/oauth/usage`` on Anthropic with the
  subscription token. Anthropic does not document this endpoint, so the
  parser reads each field with care, and a missing or unknown field never
  fails. The endpoint gives the percent used and the reset time, and no
  token count.
- ``sdk_event``: each model call on the subscription sends a
  ``RateLimitEvent``. ``note_event`` reads it with
  ``joshua_shared.limits.parse_event_windows``, which this module also
  exports, and updates the 5-hour and the weekly window, at no cost. A
  call on another server is not read.

A request reads the endpoint only when the last good read is older than
``limits.max_age``. One read runs at a time, and the other requests wait for
it. A ``429`` keeps the last snapshot and blocks a new read until its
``Retry-After`` has passed. Another failure does the same for
``ERROR_BACKOFF_S``. No caller can force a read.

The token goes in the ``Authorization`` header and nowhere else: not in a
log line, not in the answer, and not in the database. The last snapshot is
kept in the ``limits_snapshot`` table, so a restart keeps it.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from joshua_shared.contracts import (
    LimitsExtraUsage,
    LimitsResponse,
    LimitsScoped,
    LimitsWindow,
)
from joshua_shared.limits import (
    Window,
    number,
    parse_event_windows,
    pct,
    text,
)
from joshua_shared.log import get_logger

logger = get_logger("limits")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_BETA = "oauth-2025-04-20"
TIMEOUT_S = 10.0
# The wait after a failure that is not a 429, or a 429 with no usable Retry-After.
ERROR_BACKOFF_S = 60
NO_TOKEN = "no subscription token"
NOT_YET = "the limits are not known yet"
SOURCE_API = "usage_api"
SOURCE_SDK = "sdk_event"

# The variable that holds the subscription token in the CLI environment of a call.
_OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
# The ``kind`` of an entry in the ``limits`` list of the usage endpoint.
_KIND_FIVE_HOUR = "session"
_KIND_SEVEN_DAY = "weekly_all"
_KIND_SCOPED = "weekly_scoped"

Clock = Callable[[], datetime]
TokenSource = Callable[[], str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def uses_subscription(env: Mapping[str, str] | None) -> bool:
    """True when the CLI environment ``env`` of a call holds the subscription
    token, so the call runs on the subscription."""
    return bool(env and env.get(_OAUTH_ENV))


# ── the snapshot ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scoped:
    name: str
    used_pct: float
    resets_at: datetime | None = None


@dataclass(frozen=True)
class ExtraUsage:
    enabled: bool
    used: float | None = None
    limit: float | None = None
    currency: str | None = None


@dataclass(frozen=True)
class Snapshot:
    source: str
    observed_at: datetime
    five_hour: Window | None = None
    seven_day: Window | None = None
    scoped: tuple[Scoped, ...] = ()
    extra_usage: ExtraUsage | None = None
    # When the usage endpoint last answered with this data, or None. A request
    # reads the endpoint again when this is older than ``limits.max_age``.
    api_observed_at: datetime | None = field(default=None)


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


def _limit_entries(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    entries = body.get("limits")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, Mapping)]


def _entry_of(entries: list[Mapping[str, Any]], kind: str) -> Mapping[str, Any] | None:
    for entry in entries:
        if entry.get("kind") == kind:
            return entry
    return None


def _api_window(value: Any, entry: Mapping[str, Any] | None) -> Window | None:
    """A window from the ``five_hour`` or ``seven_day`` object, with the
    ``severity`` of its ``limits`` entry as the status. With no object, the
    ``percent`` of the entry."""
    status = _text(entry.get("severity")) if entry is not None else None
    if isinstance(value, Mapping):
        used = _number(value.get("utilization"))
        if used is not None:
            return Window(_pct(used), _iso_time(value.get("resets_at")), status)
    if entry is not None:
        used = _number(entry.get("percent"))
        if used is not None:
            return Window(_pct(used), _iso_time(entry.get("resets_at")), status)
    return None


def _scope_name(scope: Any) -> str | None:
    """The name of a scoped limit: the model, else the surface."""
    if not isinstance(scope, Mapping):
        return None
    for key in ("model", "surface"):
        part = scope.get(key)
        if isinstance(part, Mapping):
            name = _text(part.get("display_name")) or _text(part.get("id"))
            if name:
                return name
        elif _text(part):
            return _text(part)
    return None


def _api_scoped(body: Mapping[str, Any], entries: list[Mapping[str, Any]]) -> tuple[Scoped, ...]:
    """The weekly limits of one model: each ``weekly_scoped`` entry, then each
    ``seven_day_<name>`` object that no entry already gave."""
    found: list[Scoped] = []
    for entry in entries:
        if entry.get("kind") != _KIND_SCOPED:
            continue
        name = _scope_name(entry.get("scope"))
        used = _number(entry.get("percent"))
        if name is None or used is None:
            continue
        found.append(Scoped(name, _pct(used), _iso_time(entry.get("resets_at"))))
    names = {item.name.lower() for item in found}
    for key, value in body.items():
        if not key.startswith("seven_day_") or not isinstance(value, Mapping):
            continue
        name = key.removeprefix("seven_day_")
        used = _number(value.get("utilization"))
        if not name or used is None or name.lower() in names:
            continue
        found.append(Scoped(name.capitalize(), _pct(used), _iso_time(value.get("resets_at"))))
        names.add(name.lower())
    return tuple(found)


def _api_extra(value: Any) -> ExtraUsage | None:
    """The paid usage. ``monthly_limit`` and ``used_credits`` are in minor
    units, and ``decimal_places`` says how many. With no ``decimal_places``,
    the amounts are None: core does not guess the unit."""
    if not isinstance(value, Mapping):
        return None
    enabled = value.get("is_enabled")
    if not isinstance(enabled, bool):
        return None
    places = value.get("decimal_places")
    used = limit = None
    if isinstance(places, int) and not isinstance(places, bool) and 0 <= places <= 6:
        scale = 10**places
        raw_used = _number(value.get("used_credits"))
        raw_limit = _number(value.get("monthly_limit"))
        used = round(raw_used / scale, places) if raw_used is not None else None
        limit = round(raw_limit / scale, places) if raw_limit is not None else None
    return ExtraUsage(enabled, used, limit, _text(value.get("currency")))


def parse_usage(body: Any, observed_at: datetime) -> Snapshot | None:
    """A snapshot from the body of the usage endpoint, or None for a body that
    is not a JSON object. A missing or unknown field is skipped."""
    if not isinstance(body, Mapping):
        return None
    entries = _limit_entries(body)
    return Snapshot(
        source=SOURCE_API,
        observed_at=observed_at,
        five_hour=_api_window(body.get("five_hour"), _entry_of(entries, _KIND_FIVE_HOUR)),
        seven_day=_api_window(body.get("seven_day"), _entry_of(entries, _KIND_SEVEN_DAY)),
        scoped=_api_scoped(body, entries),
        extra_usage=_api_extra(body.get("extra_usage")),
        api_observed_at=observed_at,
    )


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
        "source": snapshot.source,
        "observed_at": _dump_time(snapshot.observed_at),
        "api_observed_at": _dump_time(snapshot.api_observed_at),
        "five_hour": _dump_window(snapshot.five_hour),
        "seven_day": _dump_window(snapshot.seven_day),
        "scoped": [
            {"name": s.name, "used_pct": s.used_pct, "resets_at": _dump_time(s.resets_at)}
            for s in snapshot.scoped
        ],
        "extra_usage": (
            None
            if snapshot.extra_usage is None
            else {
                "enabled": snapshot.extra_usage.enabled,
                "used": snapshot.extra_usage.used,
                "limit": snapshot.extra_usage.limit,
                "currency": snapshot.extra_usage.currency,
            }
        ),
    }


def _load_window(value: Any) -> Window | None:
    if not isinstance(value, Mapping):
        return None
    used = _number(value.get("used_pct"))
    if used is None:
        return None
    return Window(used, _iso_time(value.get("resets_at")), _text(value.get("status")))


def load_snapshot(data: Any) -> Snapshot | None:
    """A snapshot from ``dump_snapshot`` data, or None for data it cannot read."""
    if not isinstance(data, Mapping):
        return None
    source = data.get("source")
    observed_at = _iso_time(data.get("observed_at"))
    if source not in (SOURCE_API, SOURCE_SDK) or observed_at is None:
        return None
    scoped = []
    for item in data.get("scoped") or []:
        if isinstance(item, Mapping) and _text(item.get("name")):
            used = _number(item.get("used_pct"))
            if used is not None:
                scoped.append(Scoped(item["name"], used, _iso_time(item.get("resets_at"))))
    extra = data.get("extra_usage")
    extra_usage = None
    if isinstance(extra, Mapping) and isinstance(extra.get("enabled"), bool):
        extra_usage = ExtraUsage(
            extra["enabled"],
            _number(extra.get("used")),
            _number(extra.get("limit")),
            _text(extra.get("currency")),
        )
    return Snapshot(
        source=source,
        observed_at=observed_at,
        five_hour=_load_window(data.get("five_hour")),
        seven_day=_load_window(data.get("seven_day")),
        scoped=tuple(scoped),
        extra_usage=extra_usage,
        api_observed_at=_iso_time(data.get("api_observed_at")),
    )


class LimitsStore(Protocol):
    """The two ``Repo`` methods that keep the last snapshot."""

    async def load_limits_snapshot(self) -> dict[str, Any] | None: ...

    async def save_limits_snapshot(self, data: dict[str, Any]) -> None: ...


def _retry_after_seconds(value: str | None) -> int | None:
    """The seconds of a ``Retry-After`` header, or None. An HTTP date is not read."""
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return math.ceil(seconds)


class LimitsTracker:
    """The snapshot, the reads of the usage endpoint, and the SDK fill-in."""

    def __init__(
        self,
        *,
        token: TokenSource,
        timezone: str,
        max_age_s: int,
        store: LimitsStore | None = None,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._zone = ZoneInfo(timezone)
        self._max_age_s = max_age_s
        self._store = store
        self._clock = clock or _utc_now
        self._transport = transport
        self._lock = asyncio.Lock()
        self._snapshot: Snapshot | None = None
        self._stale = False
        self._blocked_until: datetime | None = None
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

    # --- the usage endpoint ----------------------------------------------------

    def _due(self, now: datetime) -> bool:
        if self._blocked_until is not None and now < self._blocked_until:
            return False
        last = self._snapshot.api_observed_at if self._snapshot is not None else None
        return last is None or (now - last).total_seconds() >= self._max_age_s

    def _fail(self, now: datetime, wait_s: int, reason: str) -> None:
        self._stale = True
        self._blocked_until = datetime.fromtimestamp(now.timestamp() + wait_s, UTC)
        logger.warning({"message": "limits read failed", "reason": reason, "retry_after": wait_s})

    async def _read(self, token: str) -> None:
        """One read of the usage endpoint. It never raises."""
        now = self._clock()
        headers = {"Authorization": f"Bearer {token}", "anthropic-beta": USAGE_BETA}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=self._transport) as client:
                response = await client.get(USAGE_URL, headers=headers)
        except httpx.TimeoutException:
            self._fail(now, ERROR_BACKOFF_S, "timeout")
            return
        except Exception as exc:  # noqa: BLE001 — a failed read is a stale answer
            self._fail(now, ERROR_BACKOFF_S, type(exc).__name__)
            return
        if response.status_code == 429:
            wait = _retry_after_seconds(response.headers.get("retry-after"))
            self._fail(now, wait if wait is not None else ERROR_BACKOFF_S, "rate limited")
            return
        if response.status_code != 200:
            self._fail(now, ERROR_BACKOFF_S, f"http {response.status_code}")
            return
        try:
            body = response.json()
        except ValueError:
            self._fail(now, ERROR_BACKOFF_S, "not json")
            return
        snapshot = parse_usage(body, now)
        if snapshot is None:
            self._fail(now, ERROR_BACKOFF_S, "not an object")
            return
        self._snapshot = snapshot
        self._stale = False
        self._blocked_until = None
        await self._save(snapshot)

    async def refresh_if_due(self) -> None:
        """Read the usage endpoint when the last good read is older than
        ``max_age`` and no block is in force. One read runs at a time."""
        token = self._token()
        if not token or not self._due(self._clock()):
            return
        async with self._lock:
            if self._due(self._clock()):
                await self._read(token)

    # --- the SDK fill-in -------------------------------------------------------

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
            current = Snapshot(source=SOURCE_SDK, observed_at=now)
        updated = replace(
            current,
            source=SOURCE_SDK,
            observed_at=now,
            five_hour=windows.get("five_hour", current.five_hour),
            seven_day=windows.get("seven_day", current.seven_day),
        )
        self._snapshot = updated
        self._stale = False
        self._save_later(updated)
        return True

    # --- the answer ------------------------------------------------------------

    def _local(self, value: datetime | None) -> str | None:
        return value.astimezone(self._zone).isoformat() if value is not None else None

    def _window(self, window: Window | None) -> LimitsWindow | None:
        if window is None:
            return None
        return LimitsWindow(
            used_pct=window.used_pct,
            left_pct=_pct(max(0.0, 100.0 - window.used_pct)),
            resets_at=self._local(window.resets_at),
            status=window.status,
        )

    def answer(self) -> LimitsResponse:
        """The answer of ``GET /v1/limits`` from what core knows now. No read."""
        now = self._clock()
        retry_after = None
        if self._blocked_until is not None and now < self._blocked_until:
            retry_after = math.ceil((self._blocked_until - now).total_seconds())
        if not self._token():
            return LimitsResponse(available=False, reason=NO_TOKEN)
        snap = self._snapshot
        if snap is None:
            return LimitsResponse(
                available=False, reason=NOT_YET, stale=self._stale, retry_after=retry_after
            )
        extra = snap.extra_usage
        return LimitsResponse(
            available=True,
            source=snap.source,  # type: ignore[arg-type]
            observed_at=self._local(snap.observed_at),
            stale=self._stale,
            five_hour=self._window(snap.five_hour),
            seven_day=self._window(snap.seven_day),
            scoped=[
                LimitsScoped(
                    name=s.name,
                    used_pct=s.used_pct,
                    left_pct=_pct(max(0.0, 100.0 - s.used_pct)),
                    resets_at=self._local(s.resets_at),
                )
                for s in snap.scoped
            ],
            extra_usage=(
                None
                if extra is None
                else LimitsExtraUsage(
                    enabled=extra.enabled,
                    used=extra.used,
                    limit=extra.limit,
                    currency=extra.currency,
                )
            ),
            retry_after=retry_after,
        )

    async def current(self) -> LimitsResponse:
        """Read the usage endpoint when it is due, then give the answer."""
        await self.refresh_if_due()
        return self.answer()


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
