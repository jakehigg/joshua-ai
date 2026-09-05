"""The inbound guard: one allowlist, rate-limit, and size check for every adapter.

Every adapter calls ``Guard.check`` before it builds a ``TurnEvent``. An unknown
sender never reaches core. A known sender is rate-limited per handle and size-
capped. The guard counts every refusal and keeps a bounded ring of the recent
refusals, so an operator can read ``address`` and enroll the sender.

The check runs these rules in order:

1. Resolve the handle to a person with ``cfg.people_by_handle``. A person on the
   roster is allowed with that ``person_id``.
2. An unknown sender in a group chat is allowed as the group when the chat is a
   configured group and either the group lists no members or the sender handle is
   one of them. The verdict then carries ``group_id`` and no ``person_id``.
3. Any other unknown sender is refused with ``unknown_sender``.
4. A handle over ``per_handle_per_minute`` (token bucket, burst equal to the rate)
   is refused with ``rate_limited``.
5. Text over ``max_text_chars`` stays allowed with ``reason="truncated"``; the
   adapter truncates to the cap. An attachment over ``max_attachment_bytes`` is
   skipped by the pipeline, not the message.
6. Every refusal increments a counter and appends to the recent ring. Both feed
   ``GET /admin/guard/stats`` and ``GET /admin/guard/recent``.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from threading import Lock

from joshua_shared.config import JoshuaConfig, Limits
from joshua_shared.log import get_logger

REASON_UNKNOWN = "unknown_sender"
REASON_RATE_LIMITED = "rate_limited"
REASON_TRUNCATED = "truncated"

# The refusal reasons the audit ring and the stats counters track.
REFUSAL_REASONS = (REASON_UNKNOWN, REASON_RATE_LIMITED)

# How many recent refusals the ring keeps.
RECENT_RING_SIZE = 200

# How many unconfigured group chats to remember. One entry for each chat, not
# one for each message, so a busy group takes one slot.
UNCONFIGURED_RING_SIZE = 50


logger = get_logger("channels.guard")


@dataclass(frozen=True)
class Verdict:
    """The result of one guard check.

    ``allowed`` is False for a refusal; ``reason`` then names it. On an allowed
    turn ``person_id`` is set for a known sender, ``group_id`` for a sender allowed
    as the group, and ``reason`` is ``"truncated"`` when the text was over the cap.
    """

    allowed: bool
    person_id: str | None = None
    group_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class UnconfiguredGroup:
    """One group chat that ``joshua.yaml`` does not list.

    Holds what an operator needs to add the group and nothing else. No message
    text and no sender handle: a person in the chat may be a stranger, and this
    record exists to name the chat, not the people in it.
    """

    channel_type: str
    chat_id: str
    chat_title: str | None
    at: float


@dataclass(frozen=True)
class Refusal:
    """One entry in the recent-refusals ring. ``address`` is the sender handle."""

    channel_type: str
    address: str
    chat_id: str
    reason: str
    at: float


class _RateLimiter:
    """A per-key token bucket. Capacity and burst equal ``per_minute``; the bucket
    refills at ``per_minute`` tokens per minute. A rate of zero or less disables
    the limit."""

    def __init__(self, per_minute: int, clock: Callable[[], float]) -> None:
        self._rate = float(per_minute)
        self._clock = clock
        # key -> [tokens, updated_at]
        self._buckets: dict[tuple[str, str], list[float]] = {}

    def allow(self, key: tuple[str, str]) -> bool:
        if self._rate <= 0:
            return True
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = [self._rate - 1.0, now]
            return True
        elapsed = now - bucket[1]
        bucket[1] = now
        bucket[0] = min(self._rate, bucket[0] + elapsed * (self._rate / 60.0))
        if bucket[0] >= 1.0:
            bucket[0] -= 1.0
            return True
        return False


class Guard:
    """The one inbound guard every adapter runs before it builds a turn."""

    def __init__(
        self,
        cfg: JoshuaConfig,
        limits: Limits,
        *,
        config_provider: Callable[[], JoshuaConfig] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = cfg
        self._limits = limits
        self._config_provider = config_provider
        self._wall_clock = wall_clock
        self._rate = _RateLimiter(limits.per_handle_per_minute, clock)
        self._counts: dict[str, int] = {reason: 0 for reason in REFUSAL_REASONS}
        self._counts[REASON_TRUNCATED] = 0
        self._recent: deque[Refusal] = deque(maxlen=RECENT_RING_SIZE)
        # Keyed by (channel_type, chat_id) so a busy chat takes one slot and the
        # log line is written one time. Insertion order is the age order.
        self._unconfigured: dict[tuple[str, str], UnconfiguredGroup] = {}
        self._lock = Lock()

    def _config(self) -> JoshuaConfig:
        """Return the current config, so a runtime roster change is honored."""
        return self._config_provider() if self._config_provider is not None else self._cfg

    def check(
        self,
        *,
        channel_type: str,
        sender_handle: str,
        chat_id: str,
        chat_kind: str,
        chat_title: str | None = None,
        text_len: int,
        attachment_bytes: int,
    ) -> Verdict:
        """Return the verdict for one inbound message. Record every refusal."""
        cfg = self._config()
        # Recorded before any refusal, and for a sender on the roster too. An
        # operator cannot configure a group whose chat id never appears.
        if chat_kind == "group" and cfg.groups_by_chat(channel_type, chat_id) is None:
            self._note_unconfigured(channel_type, chat_id, chat_title)
        person = cfg.people_by_handle(channel_type, sender_handle)
        person_id = person.id if person is not None else None
        group_id: str | None = None

        if person_id is None:
            group_id = self._group_for(cfg, channel_type, chat_id, chat_kind, sender_handle)
            if group_id is None:
                return self._refuse(channel_type, sender_handle, chat_id, REASON_UNKNOWN)

        if not self._rate.allow((channel_type, sender_handle)):
            return self._refuse(channel_type, sender_handle, chat_id, REASON_RATE_LIMITED)

        reason: str | None = None
        if text_len > self._limits.max_text_chars:
            reason = REASON_TRUNCATED
            with self._lock:
                self._counts[REASON_TRUNCATED] += 1
        # An attachment over the cap is skipped by the pipeline; the message stays.
        return Verdict(allowed=True, person_id=person_id, group_id=group_id, reason=reason)

    def refuse(
        self, *, channel_type: str, address: str, chat_id: str, reason: str = REASON_UNKNOWN
    ) -> Verdict:
        """Record a refusal that the caller decided, and return the verdict.

        A channel that resolves the sender itself calls this instead of
        :meth:`check`. The voice channel is the case: it must not hand a name it
        does not trust back to the roster lookup, so it decides, and the refusal
        still reaches the counters, the log, and ``/admin/guard/recent``.
        """
        return self._refuse(channel_type, address, chat_id, reason)

    def _group_for(
        self, cfg: JoshuaConfig, channel_type: str, chat_id: str, chat_kind: str, sender_handle: str
    ) -> str | None:
        """Return the group id an unknown sender is allowed under, or None.

        A DM never falls back to a group. A group chat admits the sender when the
        chat is a configured group and the group either lists no members or lists
        this handle. A group with no ``members`` treats anyone in the chat as the
        group.
        """
        if chat_kind != "group":
            return None
        group = cfg.groups_by_chat(channel_type, chat_id)
        if group is None:
            return None
        if group.members and sender_handle not in group.members:
            return None
        return group.id

    def _note_unconfigured(self, channel_type: str, chat_id: str, chat_title: str | None) -> None:
        """Remember a group chat that the config does not list.

        One record and one log line for each chat. The record holds no message
        text and no sender handle.
        """
        key = (channel_type, chat_id)
        with self._lock:
            if key in self._unconfigured:
                return
            if len(self._unconfigured) >= UNCONFIGURED_RING_SIZE:
                self._unconfigured.pop(next(iter(self._unconfigured)))
            self._unconfigured[key] = UnconfiguredGroup(
                channel_type=channel_type,
                chat_id=chat_id,
                chat_title=chat_title,
                at=self._wall_clock(),
            )
        # A group Joshua cannot answer as a group is as invisible to the operator
        # as a dropped message. The chat id is what `joshua.yaml` needs.
        logger.info(
            {
                "message": "group not configured",
                "channel_type": channel_type,
                "chat_id": chat_id,
                "chat_title": chat_title,
            }
        )

    def unconfigured(self) -> list[dict]:
        """The unconfigured group chats, oldest first, for the admin route."""
        with self._lock:
            return [asdict(entry) for entry in self._unconfigured.values()]

    def _refuse(self, channel_type: str, address: str, chat_id: str, reason: str) -> Verdict:
        with self._lock:
            self._counts[reason] += 1
            self._recent.append(
                Refusal(
                    channel_type=channel_type,
                    address=address,
                    chat_id=chat_id,
                    reason=reason,
                    at=self._wall_clock(),
                )
            )
        # A dropped message is silent to the sender by design, so it must not be
        # silent to the operator. The address is the handle to add to the roster;
        # `/admin/guard/recent` holds the same fields. The text is never logged.
        logger.warning(
            {
                "message": "inbound refused",
                "channel_type": channel_type,
                "reason": reason,
                "address": address,
                "chat_id": chat_id,
            }
        )
        return Verdict(allowed=False, reason=reason)

    def stats(self) -> dict[str, int]:
        """Return the refusal and truncation counters for ``/admin/guard/stats``."""
        with self._lock:
            counts = dict(self._counts)
        counts["refused_total"] = sum(counts[reason] for reason in REFUSAL_REASONS)
        return counts

    def recent(self) -> list[dict]:
        """Return the recent refusals, oldest first, for ``/admin/guard/recent``."""
        with self._lock:
            return [asdict(entry) for entry in self._recent]
