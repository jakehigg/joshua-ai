"""Reconciliation poller — the backstop for lost webhooks.

BlueBubbles fires a webhook exactly once and never retries. A pod restart, a
rollout, a dropped TCP connection, or a few seconds of network trouble loses
the message permanently. So the adapter also PULLS: every ``interval_s`` it asks
BB for messages newer than a watermark and feeds them through the SAME
normalize → dedupe → forward pipeline. Whatever the webhook already delivered is
dropped by the dedupe cache; whatever it lost gets through.

The watermark is in-memory and starts at (process start − ``lookback_s``), so a
restart re-reads the window that was likely lost while the pod was down.

The loop must NEVER die. A BB server that is asleep, updating, or wedged is the
normal case on a BlueBubbles Mac; every tick catches its own errors and the loop
comes back on the next interval.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any

from joshua_shared.log import get_logger

from joshua_channels.adapters.imessage.bb_client import BlueBubblesClient
from joshua_channels.adapters.imessage.forwarder import Forwarder

logger = get_logger("channels.imessage.reconcile")

SOURCE = "reconcile"


def now_ms() -> int:
    return int(time.time() * 1000)


def date_created_ms(row: Mapping[str, Any]) -> int:
    """``dateCreated`` as an int, 0 when absent/garbage."""
    raw = row.get("dateCreated")
    if isinstance(raw, bool) or raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def newer_than(rows: Sequence[Mapping[str, Any]], watermark_ms: int) -> list[Mapping[str, Any]]:
    """Client-side authority on "new".

    BB's ``after`` parameter is honoured on the query, but its exact semantics
    (created vs. delivered, inclusive vs. exclusive) are not contractual, so the
    watermark comparison that actually decides what gets forwarded happens here.
    """
    return [row for row in rows if date_created_ms(row) > watermark_ms]


def next_watermark(rows: Sequence[Mapping[str, Any]], watermark_ms: int) -> int:
    """Advance to the newest dateCreated seen; never move backwards."""
    return max([watermark_ms, *(date_created_ms(r) for r in rows)])


class Reconciler:
    def __init__(
        self,
        bb: BlueBubblesClient,
        forwarder: Forwarder,
        *,
        interval_s: int = 60,
        limit: int = 100,
        lookback_s: int = 300,
        watermark_ms: int | None = None,
    ) -> None:
        self._bb = bb
        self._forwarder = forwarder
        self._interval_s = interval_s
        self._limit = limit
        self.watermark_ms = (
            watermark_ms if watermark_ms is not None else now_ms() - lookback_s * 1000
        )
        self._task: asyncio.Task | None = None
        self.ticks = 0
        self.recovered = 0

    @property
    def enabled(self) -> bool:
        return self._interval_s > 0 and self._bb.configured

    def start(self) -> None:
        if not self.enabled:
            logger.info(
                {
                    "message": "reconciler disabled",
                    "interval_s": self._interval_s,
                    "bb_configured": self._bb.configured,
                }
            )
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="imessage-reconciler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def run(self) -> None:
        logger.info(
            {
                "message": "reconciler started",
                "interval_s": self._interval_s,
                "watermark_ms": self.watermark_ms,
            }
        )
        while True:
            await asyncio.sleep(self._interval_s)
            await self.tick()

    async def tick(self) -> int:
        """One poll. Returns how many rows were submitted to the pipeline.

        Swallows every error by design — see the module docstring.
        """
        self.ticks += 1
        cutoff = self.watermark_ms
        try:
            rows = await self._bb.query_messages_since(cutoff, limit=self._limit)
        except Exception as exc:  # noqa: BLE001 — a sleeping Mac is routine
            logger.warning(
                {"message": "reconcile query failed", "error": str(exc), "watermark_ms": cutoff}
            )
            return 0

        # The query is ``sort: DESC`` (a full page must hold the NEWEST
        # messages), but the pipeline is order-preserving end to end — submitting
        # in query order would hand core a backfilled conversation backwards.
        # Sort ASC before submitting so a recovered burst reaches core in the
        # order it was actually sent.
        fresh = sorted(newer_than(rows, cutoff), key=date_created_ms)
        submitted = 0
        for row in fresh:
            try:
                status = self._forwarder.submit(row, source=SOURCE)
            except Exception as exc:  # noqa: BLE001 — one bad row can't stop the tick
                logger.warning(
                    {
                        "message": "reconcile submit failed",
                        "error": str(exc),
                        "message_guid": row.get("guid"),
                    }
                )
                continue
            submitted += 1
            # "queued" straight away, or "coalescing" when it is being held for
            # its chat's window — either way the adapter accepted it.
            if not status.startswith("dropped:"):
                self.recovered += 1
                logger.info(
                    {
                        "message": "reconciler recovered a missed message",
                        "message_guid": row.get("guid"),
                    }
                )

        if len(rows) >= self._limit:
            # DESC + limit means a full page is the NEWEST ``limit`` messages
            # after the watermark; anything older than the page but newer than
            # the watermark is invisible to the next tick once we advance. A
            # chat never sends 100 messages in one interval, but if this
            # ever fires, the reconciler has a real gap.
            logger.warning(
                {
                    "message": "reconcile page was full — older messages may be skipped",
                    "limit": self._limit,
                    "watermark_ms": cutoff,
                }
            )

        self.watermark_ms = next_watermark(rows, cutoff)
        logger.debug(
            {
                "message": "reconcile tick",
                "rows": len(rows),
                "fresh": len(fresh),
                "submitted": submitted,
                "watermark_ms": self.watermark_ms,
            }
        )
        return submitted
