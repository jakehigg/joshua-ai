"""The receive path: a message from a bot, which is never a person turn.

The Telegram adapter calls ``PeerReceiver.handle`` for every message whose
sender is a bot, before the guard and before anything that can reply. This
path never posts a turn to core, never replies with text, and never logs a
body.

The order of the checks:

1. A bot that is not a paired peer (``peers[].telegram_bot_id``) is dropped,
   with one INFO line that holds only the bot id.
2. A message that is not in a private chat is dropped.
3. A peer over ``RATE_LIMIT_PER_MINUTE`` messages is dropped.
4. A document not named ``envelope.json`` is an attachment. It goes to the
   peer's staging folder under its sha256 and waits for its envelope.
5. The envelope document is size-checked before the download, then parsed.
   An ``ack`` updates the outbox and goes no further.
6. An item is refused when its id was seen before (the original ack is sent
   again), its kind is not in the peer's ``kinds``, it is expired, or an
   attachment it lists is not staged with the same sha256 and size. Each
   refused item gets an ack ``rejected``.
7. An item that passes is stored under ``peers/inbox/<id>/``, the peer gets an
   ack ``accepted`` (or ``unmatched`` when no local person matched), and
   core gets ``POST /v1/peers/received``. The item is stored before core is
   told, so a failed call loses nothing.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from joshua_shared import layout
from joshua_shared.config import JoshuaConfig, Peer
from joshua_shared.ids import is_ulid
from joshua_shared.log import get_logger
from joshua_shared.peers import (
    MAX_ATTACHMENT_BYTES,
    MAX_ENVELOPE_BYTES,
    AckEnvelope,
    EnvelopeError,
    ItemEnvelope,
    parse_envelope,
    resolve_recipient,
)

from joshua_channels.core_client import CoreClient
from joshua_channels.peers.outbox import Outbox
from joshua_channels.peers.protocol import (
    ENVELOPE_FILENAME,
    Clock,
    SendDocument,
    make_ack,
    utc_now,
)
from joshua_channels.peers.store import (
    META_NAME,
    SeenTable,
    Staging,
    item_exists,
    read_json,
    store_item,
)

logger = get_logger("channels.peers.receive")

# The most messages one peer may send in one minute. One item with ten
# attachments is eleven messages.
RATE_LIMIT_PER_MINUTE = 30
RATE_WINDOW_S = 60.0

# What ``handle`` returns. Each one is also the ``reason`` in the log line.
UNPAIRED = "unpaired_bot"
NOT_PRIVATE = "not_private"
RATE_LIMITED = "rate_limited"
NO_DOCUMENT = "no_document"
STAGED = "staged"
ATTACHMENT_TOO_LARGE = "attachment_too_large"
OVERSIZE = "oversize"
INVALID = "invalid_envelope"
ACK = "ack"
ACK_DROPPED = "ack_dropped"
REPLAY = "replay"
KIND_NOT_ALLOWED = "kind_not_allowed"
EXPIRED = "expired"
ATTACHMENT_MISMATCH = "attachment_mismatch"
ACCEPTED = "accepted"
UNMATCHED = "unmatched"
DOWNLOAD_FAILED = "download_failed"


def find_peer(cfg: JoshuaConfig, bot_id: str) -> Peer | None:
    for peer in cfg.peers:
        if peer.telegram_bot_id == bot_id:
            return peer
    return None


class RateLimit:
    """A sliding window of message times for each peer, on the monotonic clock."""

    def __init__(
        self,
        limit: int = RATE_LIMIT_PER_MINUTE,
        window_s: float = RATE_WINDOW_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_s
        self._monotonic = monotonic
        self._seen: dict[str, deque[float]] = {}

    def allow(self, peer_id: str) -> bool:
        now = self._monotonic()
        times = self._seen.setdefault(peer_id, deque())
        while times and now - times[0] >= self._window:
            times.popleft()
        if len(times) >= self._limit:
            return False
        times.append(now)
        return True


def _lenient_item_id(raw: bytes) -> str | None:
    """The id of a refused envelope, when it has one and is not an ack.

    A refused item gets an ack ``rejected``, and an ack needs the id. The
    value is used only when it is a ULID.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("kind") == "ack":
        return None
    value = data.get("id")
    return value if isinstance(value, str) and is_ulid(value) else None


class PeerReceiver:
    """Handle every message from a bot."""

    def __init__(
        self,
        *,
        data_dir: Path | str,
        settings_provider: Callable[[], JoshuaConfig],
        outbox: Outbox,
        core_client: CoreClient | None,
        clock: Clock = utc_now,
        rate_limit: RateLimit | None = None,
    ) -> None:
        self._root = Path(data_dir)
        self._settings = settings_provider
        self._outbox = outbox
        self._core = core_client
        self._clock = clock
        self._rate = rate_limit or RateLimit()
        self._seen = SeenTable(self._root)
        self._staging = Staging(self._root)
        self._lock = asyncio.Lock()

    @property
    def seen(self) -> SeenTable:
        return self._seen

    @property
    def staging(self) -> Staging:
        return self._staging

    async def handle(self, message: Any, user: Any, chat: Any, bot: Any, send: SendDocument) -> str:
        """Handle one message from a bot. Returns what happened, for the log and tests."""
        bot_id = str(user.id)
        peer = find_peer(self._settings(), bot_id)
        if peer is None:
            logger.info({"message": "peer: bot dropped", "reason": UNPAIRED, "bot_id": bot_id})
            return UNPAIRED
        if getattr(chat, "type", None) != "private":
            return self._drop(peer, NOT_PRIVATE)
        if not self._rate.allow(peer.id):
            return self._drop(peer, RATE_LIMITED)
        document = getattr(message, "document", None)
        if document is None:
            return self._drop(peer, NO_DOCUMENT)
        if document.file_name != ENVELOPE_FILENAME:
            return await self._stage(peer, document, bot)
        return await self._envelope(peer, document, bot, send)

    def _drop(self, peer: Peer, reason: str, **extra: object) -> str:
        line = {"message": "peer: message dropped", "peer": peer.id, "reason": reason}
        logger.info({**line, **extra})
        return reason

    # --- attachments --------------------------------------------------------

    async def _stage(self, peer: Peer, document: Any, bot: Any) -> str:
        size = document.file_size
        if size is None or size > MAX_ATTACHMENT_BYTES:
            return self._drop(peer, ATTACHMENT_TOO_LARGE)
        now = self._clock()
        self._staging.prune(peer.id, now)
        tmp = self._staging.new_temp(peer.id)
        try:
            tg_file = await bot.get_file(document.file_id)
            await tg_file.download_to_drive(tmp)
        except Exception as exc:  # noqa: BLE001 — a failed download stages nothing
            tmp.unlink(missing_ok=True)
            return self._drop(peer, DOWNLOAD_FAILED, error=type(exc).__name__)
        if tmp.stat().st_size > MAX_ATTACHMENT_BYTES:
            tmp.unlink(missing_ok=True)
            return self._drop(peer, ATTACHMENT_TOO_LARGE)
        digest = self._staging.commit(peer.id, tmp, now)
        logger.info({"message": "peer: attachment staged", "peer": peer.id, "sha256": digest})
        return STAGED

    # --- envelopes ----------------------------------------------------------

    async def _envelope(self, peer: Peer, document: Any, bot: Any, send: SendDocument) -> str:
        size = document.file_size
        if size is not None and size > MAX_ENVELOPE_BYTES:
            return self._drop(peer, OVERSIZE)
        try:
            tg_file = await bot.get_file(document.file_id)
            raw = bytes(await tg_file.download_as_bytearray())
        except Exception as exc:  # noqa: BLE001 — a failed download is dropped; the peer retries
            return self._drop(peer, DOWNLOAD_FAILED, error=type(exc).__name__)
        now = self._clock()
        try:
            envelope = parse_envelope(raw)
        except EnvelopeError:
            item_id = _lenient_item_id(raw) if len(raw) <= MAX_ENVELOPE_BYTES else None
            if item_id is not None:
                await self._send_ack(peer, send, make_ack(item_id, "rejected", now, None), item_id)
            reason = OVERSIZE if len(raw) > MAX_ENVELOPE_BYTES else INVALID
            return self._drop(peer, reason, id=item_id)
        if isinstance(envelope, AckEnvelope):
            applied = self._outbox.apply_ack(peer.id, envelope)
            return ACK if applied else ACK_DROPPED
        async with self._lock:
            return await self._item(peer, envelope, raw, now, send)

    async def _item(
        self, peer: Peer, envelope: ItemEnvelope, raw: bytes, now: datetime, send: SendDocument
    ) -> str:
        seen = self._seen.get(envelope.id)
        if seen is not None:
            await self._send_ack(peer, send, seen.ack, envelope.id)
            return self._drop(peer, REPLAY, id=envelope.id)
        if item_exists(self._root, envelope.id):
            # Stored, but the replay record was not written. Write it now.
            folder = layout.peer_item_dir(envelope.id, self._root)
            meta = read_json(folder / META_NAME) or {}
            status = ACCEPTED if meta.get("recipient") else UNMATCHED
            ack = make_ack(envelope.id, status, now, envelope.expires_at)
            self._seen.put(envelope.id, status, envelope.expires_at, ack)
            await self._send_ack(peer, send, ack, envelope.id)
            return self._drop(peer, REPLAY, id=envelope.id)
        if envelope.kind not in peer.kinds:
            return await self._refuse(peer, envelope, now, send, KIND_NOT_ALLOWED)
        if envelope.expired(now):
            return await self._refuse(peer, envelope, now, send, EXPIRED)
        self._staging.prune(peer.id, now)
        files = self._match_attachments(peer, envelope)
        if files is None:
            return await self._refuse(peer, envelope, now, send, ATTACHMENT_MISMATCH)

        recipient = resolve_recipient(self._settings().people, envelope.to_person)
        meta = {
            "peer_id": peer.id,
            "received_at": now.isoformat(),
            "recipient": recipient.person_id,
            "reason": recipient.reason,
            "candidates": list(recipient.candidates),
            "status": "new",
        }
        store_item(self._root, envelope, raw, files, meta)
        status = ACCEPTED if recipient.matched else UNMATCHED
        ack = make_ack(envelope.id, status, now, envelope.expires_at)
        self._seen.put(envelope.id, status, envelope.expires_at, ack)
        logger.info(
            {
                "message": "peer: item stored",
                "peer": peer.id,
                "id": envelope.id,
                "kind": envelope.kind,
                "status": status,
                "match": recipient.reason,
            }
        )
        await self._send_ack(peer, send, ack, envelope.id)
        await self._notify_core(envelope.id)
        return status

    def _match_attachments(self, peer: Peer, envelope: ItemEnvelope) -> dict[str, Path] | None:
        """Map each listed attachment to its staged file, or None when one is wrong."""
        files: dict[str, Path] = {}
        for attachment in envelope.body.attachments:
            if attachment.name in files:
                return None
            path = self._staging.path(peer.id, attachment.sha256)
            if path is None or path.stat().st_size != attachment.size:
                return None
            files[attachment.name] = path
        return files

    async def _refuse(
        self, peer: Peer, envelope: ItemEnvelope, now: datetime, send: SendDocument, reason: str
    ) -> str:
        ack = make_ack(envelope.id, "rejected", now, envelope.expires_at)
        self._seen.put(envelope.id, "rejected", envelope.expires_at, ack)
        await self._send_ack(peer, send, ack, envelope.id)
        return self._drop(peer, reason, id=envelope.id)

    async def _send_ack(self, peer: Peer, send: SendDocument, ack: bytes, ref: str) -> None:
        """Send an ack to the peer's private chat. A failure is logged; the peer retries."""
        try:
            await send(peer.telegram_bot_id, ack, ENVELOPE_FILENAME)
        except Exception as exc:  # noqa: BLE001 — the peer sends again and gets the same ack
            logger.warning(
                {
                    "message": "peer: ack send failed",
                    "peer": peer.id,
                    "id": ref,
                    "error": type(exc).__name__,
                }
            )

    async def _notify_core(self, item_id: str) -> None:
        """Tell core an item arrived. A failure is logged and the item stays."""
        if self._core is None:
            logger.warning({"message": "peer: no core client; core not told", "id": item_id})
            return
        try:
            status = await self._core.peer_received(item_id)
        except Exception as exc:  # noqa: BLE001 — the item is already stored
            logger.warning(
                {"message": "peer: core notify failed", "id": item_id, "error": type(exc).__name__}
            )
            return
        if status == 404:
            logger.info({"message": "peer: core has no peers route", "id": item_id})
        elif not 200 <= status < 300:
            logger.warning(
                {"message": "peer: core notify refused", "id": item_id, "status": status}
            )
