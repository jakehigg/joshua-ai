"""The outbox: each item this instance sends, kept until an ack or its expiry.

One folder for each item, ``peers/outbox/<id>/``:

- ``envelope.json``: the envelope document, sent as it is on every attempt;
- ``attachments/<sha256>``: a copy of each attachment, so a later attempt
  does not depend on the source file;
- ``state.json``: ``peer_id``, ``chat_id``, ``attempts``, ``next_attempt_at``,
  ``expires_at``, ``status`` (``pending``, ``acked``, ``failed``, ``expired``),
  and ``ack_status``.

An attempt sends the attachments first and the envelope last. A background
task sends each ``pending`` item again with exponential backoff until an ack
arrives or ``expires_at`` passes. An ack ``rejected`` sets ``failed``. An ack
``accepted`` or ``unmatched`` sets ``acked``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from joshua_shared import layout
from joshua_shared.log import get_logger
from joshua_shared.peers import AckEnvelope, Attachment, ItemEnvelope, dump_envelope

from joshua_channels.peers.protocol import ENVELOPE_FILENAME, Clock, SendDocument, utc_now
from joshua_channels.peers.store import parse_time, read_json, write_json

logger = get_logger("channels.peers.outbox")

# The wait after the first attempt. Each attempt doubles it, up to the cap.
RETRY_BASE_S = 60
RETRY_MAX_S = 3600

# How often the background task looks for an item that is due.
RETRY_TICK_S = 30

STATE_NAME = "state.json"
ATTACHMENTS_NAME = "attachments"

PENDING = "pending"
ACKED = "acked"
FAILED = "failed"
EXPIRED = "expired"


def backoff(attempts: int) -> timedelta:
    """The wait after attempt number ``attempts`` (1 for the first)."""
    seconds = RETRY_BASE_S * (2 ** max(0, attempts - 1))
    return timedelta(seconds=min(seconds, RETRY_MAX_S))


class OutboxExists(Exception):
    """The outbox already holds an item with this id."""


class Outbox:
    """The file-based outbox under ``peers/outbox/``."""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        sender: Callable[[], SendDocument | None],
        clock: Clock = utc_now,
    ) -> None:
        self._root = Path(data_dir)
        self._sender = sender
        self._clock = clock

    # --- paths and state ----------------------------------------------------

    def _dir(self, item_id: str) -> Path:
        return layout.peer_outbox_dir(item_id, self._root)

    def state(self, item_id: str) -> dict[str, Any] | None:
        try:
            return read_json(self._dir(item_id) / STATE_NAME)
        except ValueError:
            return None

    def _write_state(self, item_id: str, state: dict[str, Any]) -> None:
        write_json(self._dir(item_id) / STATE_NAME, state)

    def ids(self) -> list[str]:
        root = layout.peers_outbox_root(self._root)
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))

    # --- enqueue ------------------------------------------------------------

    def enqueue(
        self,
        peer_id: str,
        chat_id: str,
        envelope: ItemEnvelope,
        files: list[tuple[Attachment, Path]],
    ) -> None:
        """Record one item as ``pending`` and due now. Raises ``OutboxExists``."""
        final = self._dir(envelope.id)
        if final.exists():
            raise OutboxExists(envelope.id)
        final.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f".tmp-{envelope.id}-", dir=final.parent))
        try:
            (work / ENVELOPE_FILENAME).write_bytes(dump_envelope(envelope))
            if files:
                folder = work / ATTACHMENTS_NAME
                folder.mkdir()
                for attachment, source in files:
                    shutil.copyfile(source, folder / attachment.sha256)
            now = self._clock()
            state = {
                "peer_id": peer_id,
                "chat_id": chat_id,
                "attempts": 0,
                "next_attempt_at": now.isoformat(),
                "expires_at": envelope.expires_at.isoformat(),
                "status": PENDING,
                "ack_status": None,
            }
            write_json(work / STATE_NAME, state)
            if final.exists():
                raise OutboxExists(envelope.id)
            os.replace(work, final)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        logger.info({"message": "peer item queued", "id": envelope.id, "peer": peer_id})

    # --- send ---------------------------------------------------------------

    async def attempt(self, item_id: str) -> bool:
        """Send one item once: attachments first, the envelope last.

        Returns True when every document went out. Either way the attempt
        counts, and the next one is due after ``backoff``. An item that is not
        ``pending`` is not sent.
        """
        state = self.state(item_id)
        if state is None or state.get("status") != PENDING:
            return False
        folder = self._dir(item_id)
        ok = False
        send = self._sender()
        if send is None:
            logger.warning({"message": "peer send: telegram is not running", "id": item_id})
        else:
            try:
                raw = (folder / ENVELOPE_FILENAME).read_bytes()
                envelope = ItemEnvelope.model_validate_json(raw)
                chat_id = str(state["chat_id"])
                for attachment in envelope.body.attachments:
                    data = (folder / ATTACHMENTS_NAME / attachment.sha256).read_bytes()
                    await send(chat_id, data, attachment.name)
                await send(chat_id, raw, ENVELOPE_FILENAME)
                ok = True
            except Exception as exc:  # noqa: BLE001 — a failed send waits for the next attempt
                logger.warning(
                    {"message": "peer send failed", "id": item_id, "error": type(exc).__name__}
                )
        # Read the state again: an ack can arrive while the documents go out.
        latest = self.state(item_id)
        if latest is None or latest.get("status") != PENDING:
            return ok
        attempts = int(latest.get("attempts", 0)) + 1
        latest["attempts"] = attempts
        latest["next_attempt_at"] = (self._clock() + backoff(attempts)).isoformat()
        self._write_state(item_id, latest)
        logger.info({"message": "peer send attempt", "id": item_id, "attempt": attempts, "ok": ok})
        return ok

    async def retry_due(self) -> int:
        """Expire each pending item past ``expires_at``, and send each one that is due.

        Returns the number of attempts made.
        """
        now = self._clock()
        made = 0
        for item_id in self.ids():
            state = self.state(item_id)
            if state is None or state.get("status") != PENDING:
                continue
            expires_at = parse_time(state.get("expires_at"))
            if expires_at is None or now >= expires_at:
                state["status"] = EXPIRED
                self._write_state(item_id, state)
                logger.info({"message": "peer item expired with no ack", "id": item_id})
                continue
            due = parse_time(state.get("next_attempt_at"))
            if due is None or now >= due:
                await self.attempt(item_id)
                made += 1
        return made

    def prune(self) -> int:
        """Remove each item that is no longer pending and is past ``expires_at``."""
        now = self._clock()
        removed = 0
        for item_id in self.ids():
            state = self.state(item_id)
            if state is None or state.get("status") == PENDING:
                continue
            expires_at = parse_time(state.get("expires_at"))
            if expires_at is None or now >= expires_at:
                shutil.rmtree(self._dir(item_id), ignore_errors=True)
                removed += 1
        return removed

    # --- ack ----------------------------------------------------------------

    def apply_ack(self, peer_id: str, ack: AckEnvelope) -> bool:
        """Record an ack. Returns False, and changes nothing, when it is dropped.

        The ack is dropped when ``ref`` is not in this outbox, when the item
        went to another peer, or when an earlier ack already settled it.
        """
        ref = ack.body.ref
        state = self.state(ref)
        if state is None:
            logger.info({"message": "peer ack dropped: unknown ref", "peer": peer_id})
            return False
        if state.get("peer_id") != peer_id:
            logger.info({"message": "peer ack dropped: wrong peer", "peer": peer_id, "id": ref})
            return False
        if state.get("ack_status") is not None:
            logger.info({"message": "peer ack dropped: already settled", "id": ref})
            return False
        state["ack_status"] = ack.body.status
        state["status"] = FAILED if ack.body.status == "rejected" else ACKED
        self._write_state(ref, state)
        logger.info({"message": "peer ack", "id": ref, "status": ack.body.status})
        return True


async def retry_loop(
    tick: Callable[[], Awaitable[object]],
    *,
    interval_s: float = RETRY_TICK_S,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> None:
    """Call ``tick`` every ``interval_s`` seconds until cancelled. A failed tick is logged."""
    while True:
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad tick must not stop the loop
            logger.warning({"message": "peer retry tick failed", "error": type(exc).__name__})
        await sleep(interval_s)
