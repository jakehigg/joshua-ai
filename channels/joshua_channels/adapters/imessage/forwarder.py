"""Inbound pipeline: normalize → dedupe → coalesce → queue → guard → turn.

``submit()`` is the front door both producers use (the webhook route and the
reconciliation poller). It runs the pure guards, the staleness check and the
dedupe check, then hands the message to the coalescer and returns — the webhook
handler MUST NOT block, because BlueBubbles never retries a webhook it failed to
deliver.

The coalescer is a per-``(chat_guid, sender_address)`` debounce. Messages.app
splits "prose + a link" into two messages ~250ms apart, and people type in
bursts; both arrive as separate webhooks and, without this, become two separate
turns in core. Holding each chat for the coalesce window and merging what lands
inside it turns them back into one. The buffer is per chat and flushed by its
own timer, so a chat mid-window never delays a different chat.

``run()`` is a SINGLE worker task. One worker, not a pool: messages
must reach core in the order they were sent, and a photo download in message A
must not let message B overtake it.

``deliver()`` applies the inbound ``Guard``, downloads attachment blobs, runs
the attachment pipeline, and submits one ``TurnEvent`` to core. A refused sender
never reaches core. Delivery is fire-and-forget: a core failure is logged with
the message guid and dropped; the reconciler and the dedupe TTL bring a lost
message back on their own.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from joshua_shared.config import JoshuaConfig
from joshua_shared.contracts import Attachment as TurnAttachment
from joshua_shared.contracts import Chat, Handle, TurnEvent
from joshua_shared.log import get_logger

from joshua_channels.adapters.imessage.bb_client import BlueBubblesClient, BlueBubblesError
from joshua_channels.adapters.imessage.dedupe import GuidDedupe
from joshua_channels.adapters.imessage.normalize import (
    DROP_DUPLICATE,
    DROP_NO_CHAT,
    DROP_STALE,
    InboundMessage,
    Rejected,
    normalize_message,
)
from joshua_channels.core_client import CoreClient
from joshua_channels.guard import REASON_TRUNCATED, REASON_UNKNOWN, Guard

logger = get_logger("channels.imessage.forwarder")

CHANNEL_TYPE = "imessage"

# The text a message with an attachment and no caption carries to core.
NO_CAPTION_TEXT = "(file attached — no caption)"

# The reply an unknown sender gets when the iMessage policy is ``reply``.
UNKNOWN_SENDER_REPLY = "Sorry, I do not know you, so I cannot help here."

# Bounded so a core outage can't grow the queue without limit. On overflow the
# oldest-loses policy is deliberate: the reconciler re-offers whatever dropped.
DEFAULT_QUEUE_MAX = 1000

# What the merged text is joined with. A newline, not a space: the two halves
# were sent as separate messages and core should see them as separate lines.
COALESCE_JOINER = "\n"

CoalesceKey = tuple[str, str]


@runtime_checkable
class AttachmentPipeline(Protocol):
    """Turn downloaded files into ``Attachment`` records.

    The adapter downloads BlueBubbles blobs into ``inbox_dir`` and hands the
    directory over; the pipeline returns one ``Attachment`` per stored file,
    each ``path`` files-MCP relative (``attachments/YYYY/MM/<file>``).
    """

    async def process(
        self, inbox_dir: Path, *, person_id: str | None, group_id: str | None
    ) -> list[TurnAttachment]: ...


# ── pure helpers ─────────────────────────────────────────────────────────────


def is_stale(date_created_ms: int, *, now_s: float, max_age_s: float) -> bool:
    """Guard 6. Pure given the clock reading.

    A ``date_created`` of 0 means BB did not tell us when the message was sent
    (it is absent from some payload shapes). Unknown is NOT old — dropping
    those would be a silent loss, and a duplicate is always the better failure.
    """
    if max_age_s <= 0 or date_created_ms <= 0:
        return False
    return (now_s - date_created_ms / 1000.0) > max_age_s


def coalesce_key(message: InboundMessage) -> CoalesceKey:
    """One buffer per conversation *and* speaker. Two people typing into the
    same group at once must not have their sentences spliced together."""
    return (message.chat_guid, message.sender_address)


def merge_messages(first: InboundMessage, later: InboundMessage) -> InboundMessage:
    """Older + newer → one message. Pure.

    The result IS the first message, extended: it keeps its ``message_guid``
    (core's idempotency key) and its ``date_created`` (when the turn started).
    Text is joined oldest-first, empty parts skipped. Attachments are
    concatenated in the same order and re-indexed contiguously.
    """
    parts = [part for part in (first.text, later.text) if part]
    attachments = tuple(
        replace(att, index=index) for index, att in enumerate(first.attachments + later.attachments)
    )
    return replace(first, text=COALESCE_JOINER.join(parts), attachments=attachments)


@dataclass
class Batch:
    """What actually goes on the queue: a message plus every guid that folded
    into it, so dedupe can mark them all handled."""

    message: InboundMessage
    source: str
    guids: list[str] = field(default_factory=list)
    deadline: float = 0.0

    @property
    def merged(self) -> bool:
        return len(self.guids) > 1


class Coalescer:
    """Per-key debounce buffer. Pure apart from an injected clock — it holds no
    tasks and touches no queue; the Forwarder owns the timers."""

    def __init__(
        self, window_s: float = 0.0, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.window_s = window_s
        self._clock = clock
        self._pending: dict[CoalesceKey, Batch] = {}

    @property
    def enabled(self) -> bool:
        return self.window_s > 0

    def __len__(self) -> int:
        return len(self._pending)

    def add(self, message: InboundMessage, *, source: str) -> Batch | None:
        """Returns a Batch to enqueue NOW, or None when the message is held.

        With the window disabled this is a pass-through, so the pipeline is the
        behaviour it had before coalescing existed.
        """
        if not self.enabled:
            return Batch(message, source, [message.message_guid], self._clock())

        key = coalesce_key(message)
        held = self._pending.get(key)
        deadline = self._clock() + self.window_s
        if held is None:
            self._pending[key] = Batch(message, source, [message.message_guid], deadline)
        else:
            # Every arrival restarts the window: a burst is coalesced until the
            # sender actually stops typing.
            self._pending[key] = Batch(
                merge_messages(held.message, message),
                held.source,
                [*held.guids, message.message_guid],
                deadline,
            )
        return None

    def pop(self, key: CoalesceKey) -> Batch | None:
        return self._pending.pop(key, None)

    def pop_due(self, now: float | None = None) -> list[Batch]:
        """Every batch whose window has expired, oldest deadline first."""
        moment = self._clock() if now is None else now
        due = sorted(
            (k for k, b in self._pending.items() if b.deadline <= moment),
            key=lambda k: self._pending[k].deadline,
        )
        return [self._pending.pop(key) for key in due]

    def pop_all(self) -> list[Batch]:
        """Everything held, oldest deadline first (shutdown)."""
        batches = sorted(self._pending.values(), key=lambda b: b.deadline)
        self._pending.clear()
        return batches


class Forwarder:
    """Owns the inbound queue, the dedupe cache, the guard, and the turn to core."""

    def __init__(
        self,
        bb: BlueBubblesClient,
        *,
        guard: Guard,
        core_client: CoreClient | None,
        settings_provider: Callable[[], JoshuaConfig],
        pipeline: AttachmentPipeline | None = None,
        data_dir: str = "/data",
        max_attachment_bytes: int = 26214400,
        dedupe: GuidDedupe | None = None,
        queue_maxsize: int = DEFAULT_QUEUE_MAX,
        coalesce_window_s: float = 0.0,
        stale_max_age_s: float = 0.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._bb = bb
        self._guard = guard
        self._core = core_client
        self._settings_provider = settings_provider
        self._pipeline = pipeline
        self._data_dir = Path(data_dir)
        self._max_attachment_bytes = max_attachment_bytes
        self._dedupe = dedupe if dedupe is not None else GuidDedupe()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_maxsize)
        self._task: asyncio.Task | None = None
        self._stale_max_age_s = stale_max_age_s
        self._clock = clock
        self._coalescer = Coalescer(coalesce_window_s, clock=time.monotonic)
        self._timers: dict[CoalesceKey, asyncio.Task] = {}
        # Counters, surfaced in logs and asserted in tests.
        self.forwarded = 0
        self.failed = 0
        self.dropped = 0
        self.coalesced = 0
        self.refused = 0

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="imessage-forwarder")

    async def stop(self) -> None:
        # Push whatever is mid-window onto the queue first: a rollout should not
        # swallow a half-typed turn.
        self.flush_pending()
        timers = list(self._timers.values())
        self._timers.clear()
        for timer in timers:
            timer.cancel()
        for timer in timers:
            try:
                await timer
            except asyncio.CancelledError:
                pass
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def drain(self) -> None:
        """Wait for everything currently queued to be handled (tests/shutdown).

        Only the QUEUE — anything still inside its coalesce window is not queued
        yet. Call ``flush_pending()`` first to include it.
        """
        await self._queue.join()

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def pending(self) -> int:
        """Messages held inside a coalesce window, not yet queued."""
        return len(self._coalescer)

    # ── producer side ────────────────────────────────────────────────────

    def submit(self, data: object, *, source: str) -> str:
        """Guards + dedupe + coalesce/enqueue. Returns a status for the caller.

        Never raises and never awaits I/O — the webhook route calls this on the
        request path. Every drop logs ONE structured line at INFO carrying the
        reason and the guid, so a lost webhook and a correct drop are
        distinguishable in prod.
        """
        result = normalize_message(data, max_attachment_bytes=self._max_attachment_bytes)
        if isinstance(result, Rejected):
            return self._drop(result.reason, result.guid, source, detail=result.detail)

        if is_stale(result.date_created, now_s=self._clock(), max_age_s=self._stale_max_age_s):
            # A webhook BB replayed hours later. Past the dedupe TTL nothing
            # remembers that this turn already ran, so it would run again.
            return self._drop(
                DROP_STALE,
                result.message_guid,
                source,
                detail=f"date_created={result.date_created}",
            )

        if self._dedupe.seen(result.message_guid):
            return self._drop(DROP_DUPLICATE, result.message_guid, source)

        batch = self._coalescer.add(result, source=source)
        if batch is not None:
            return self._enqueue(batch)

        armed = self._arm_timer(coalesce_key(result))
        if armed is not None:
            # No event loop to time the window with — the fallback already
            # queued it, so report what actually happened.
            return armed
        logger.info(
            {
                "message": "inbound coalescing",
                "message_guid": result.message_guid,
                "chat_guid": result.chat_guid,
                "window_s": self._coalescer.window_s,
                "source": source,
            }
        )
        return "coalescing"

    def _drop(self, reason: str, guid: str, source: str, *, detail: str = "") -> str:
        self.dropped += 1
        logger.info(
            {
                "message": "inbound dropped",
                "reason": reason,
                "message_guid": guid,
                "detail": detail,
                "source": source,
            }
        )
        if reason == DROP_NO_CHAT:
            logger.warning(
                {
                    "message": "inbound message has no chat guid",
                    "message_guid": guid,
                    "source": source,
                }
            )
        return f"dropped:{reason}"

    def _enqueue(self, batch: Batch) -> str:
        message = batch.message
        # Several guids can fold into one forwarded message; only the first one
        # names the result, so mark the rest handled or the reconciler re-offers
        # them.
        for guid in batch.guids:
            self._dedupe.record(guid)
        try:
            self._queue.put_nowait((message, batch.source))
        except asyncio.QueueFull:
            self.dropped += 1
            logger.error(
                {
                    "message": "inbound queue full — message dropped",
                    "message_guid": message.message_guid,
                    "source": batch.source,
                }
            )
            return "dropped:queue_full"

        if batch.merged:
            self.coalesced += 1
        logger.info(
            {
                "message": "inbound queued",
                "message_guid": message.message_guid,
                "chat_guid": message.chat_guid,
                "is_group": message.is_group,
                "attachments": len(message.attachments),
                "source": batch.source,
                "depth": self._queue.qsize(),
                "merged_guids": batch.guids if batch.merged else None,
            }
        )
        return "queued"

    # ── coalesce timers (the only stateful edge the Coalescer doesn't own) ─

    def _arm_timer(self, key: CoalesceKey) -> str | None:
        """(Re)start this chat's flush timer. Per key, so a chat mid-window
        never holds up another chat.

        Returns None once the timer is armed, or the enqueue status when there
        was no event loop to arm it with.
        """
        existing = self._timers.pop(key, None)
        if existing is not None:
            existing.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No event loop (a sync caller): behave as if the window had already
            # expired rather than hold the message forever.
            return self._flush_key(key)
        self._timers[key] = loop.create_task(self._flush_after(key))
        return None

    async def _flush_after(self, key: CoalesceKey) -> None:
        await asyncio.sleep(self._coalescer.window_s)
        self._timers.pop(key, None)
        self._flush_key(key)

    def _flush_key(self, key: CoalesceKey) -> str | None:
        batch = self._coalescer.pop(key)
        return None if batch is None else self._enqueue(batch)

    def flush_pending(self) -> int:
        """Queue everything still inside a coalesce window, now. Returns how
        many batches moved (shutdown, and tests that don't want to sleep)."""
        batches = self._coalescer.pop_all()
        for batch in batches:
            self._enqueue(batch)
        return len(batches)

    # ── worker side ──────────────────────────────────────────────────────

    async def run(self) -> None:
        while True:
            message, source = await self._queue.get()
            try:
                await self.deliver(message, source=source)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the worker must never die
                self.failed += 1
                logger.error(
                    {
                        "message": "forwarding failed",
                        "message_guid": message.message_guid,
                        "source": source,
                        "error": str(exc),
                    }
                )
            finally:
                self._queue.task_done()

    async def deliver(self, message: InboundMessage, *, source: str = "webhook") -> bool:
        """Apply the guard, run the attachment pipeline, submit one turn to core."""
        cfg = self._settings_provider()
        verdict = self._guard.check(
            channel_type=CHANNEL_TYPE,
            sender_handle=message.sender_address,
            chat_id=message.chat_guid,
            chat_kind="group" if message.is_group else "dm",
            text_len=len(message.text),
            attachment_bytes=_declared_bytes(message),
        )
        if not verdict.allowed:
            self.refused += 1
            await self._on_refused(message.chat_guid, verdict.reason, cfg)
            return False

        text = message.text
        if verdict.reason == REASON_TRUNCATED:
            text = text[: cfg.channels.limits.max_text_chars]

        attachments = await self._collect_attachments(message, verdict.person_id, verdict.group_id)
        if not text:
            text = NO_CAPTION_TEXT

        event = TurnEvent(
            channel=f"{CHANNEL_TYPE}:{message.chat_guid}",
            chat=Chat(
                kind="group" if message.is_group else "dm",
                id=message.chat_guid,
                title=message.chat_display_name,
            ),
            handle=Handle(type=CHANNEL_TYPE, id=message.sender_address),
            text=text,
            attachments=attachments,
            message_id=message.message_guid,
        )
        return await self._submit(event, source=source)

    async def _submit(self, event: TurnEvent, *, source: str) -> bool:
        """Post the turn to core. Fire-and-forget: a failure is logged and dropped."""
        if self._core is None:
            logger.warning({"message": "no core client; turn dropped", "channel": event.channel})
            self.failed += 1
            return False
        try:
            ack = await self._core.submit_turn(event)
        except Exception as exc:  # noqa: BLE001 — a core outage is not our failure to retry
            self.failed += 1
            logger.error(
                {
                    "message": "submit_turn raised",
                    "channel": event.channel,
                    "source": source,
                    "error": str(exc),
                }
            )
            return False
        if ack.accepted:
            self.forwarded += 1
            logger.info(
                {"message": "forwarded to core", "channel": event.channel, "source": source}
            )
            return True
        self.failed += 1
        logger.warning(
            {
                "message": "turn not accepted",
                "channel": event.channel,
                "source": source,
                "status": ack.status,
                "reason": ack.reason,
            }
        )
        return False

    async def _on_refused(self, chat_guid: str, reason: str | None, cfg: JoshuaConfig) -> None:
        """Apply the ``unknown_sender`` policy. Rate-limit refusals stay silent,
        and iMessage never replies to an unknown sender unless configured."""
        imessage_cfg = cfg.channels.imessage
        policy = imessage_cfg.unknown_sender if imessage_cfg is not None else "drop"
        if reason == REASON_UNKNOWN and policy == "reply":
            try:
                await self._bb.send_chunks(chat_guid, [UNKNOWN_SENDER_REPLY])
            except BlueBubblesError as exc:
                logger.warning(
                    {
                        "message": "unknown-sender reply failed",
                        "chat_guid": chat_guid,
                        "error": str(exc),
                    }
                )

    async def _collect_attachments(
        self, message: InboundMessage, person_id: str | None, group_id: str | None
    ) -> list[TurnAttachment]:
        """Download every non-skipped attachment into the inbox, then hand the
        directory to the pipeline.

        A download failure drops that one attachment and lets the message
        through: an unreadable photo must not cost us the text that came with it.
        """
        downloadable = [att for att in message.attachments if not att.skipped]
        if not downloadable:
            return []
        if self._pipeline is None:
            logger.warning(
                {
                    "message": "no attachment pipeline; attachments skipped",
                    "message_guid": message.message_guid,
                }
            )
            return []
        inbox_dir = self._data_dir / "inbox" / message.message_guid
        inbox_dir.mkdir(parents=True, exist_ok=True)
        wrote_any = False
        for att in downloadable:
            try:
                blob = await self._bb.download_attachment(att.guid)
            except Exception as exc:  # noqa: BLE001 — BlueBubblesError + transport
                logger.warning(
                    {
                        "message": "attachment download failed",
                        "message_guid": message.message_guid,
                        "attachment_guid": att.guid,
                        "error": str(exc),
                    }
                )
                continue
            (inbox_dir / att.filename).write_bytes(blob)
            wrote_any = True
        if not wrote_any:
            return []
        return await self._pipeline.process(inbox_dir, person_id=person_id, group_id=group_id)


def _declared_bytes(message: InboundMessage) -> int:
    """The largest declared attachment size, without downloading."""
    return max((att.size or 0 for att in message.attachments), default=0)
