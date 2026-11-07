"""The wire rules of the peer path, shared by the receiver and the outbox.

One envelope is one document named ``envelope.json``. The attachments of an
item go first, one document each, and the envelope goes last.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from joshua_shared.ids import new_ulid
from joshua_shared.peers import ENVELOPE_VERSION, AckBody, AckEnvelope, dump_envelope

# The file name that marks a document as the envelope. Any other document from
# a peer is an attachment.
ENVELOPE_FILENAME = "envelope.json"
ENVELOPE_MIME = "application/json"

# The shortest life an ack gets. An ack lives at least as long as the item it
# answers, so a resent ack is still valid.
ACK_MIN_TTL = timedelta(days=1)

Clock = Callable[[], datetime]

# Send one document to a Telegram chat: ``(chat_id, data, filename)``.
SendDocument = Callable[[str, bytes, str], Awaitable[None]]


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(now: datetime) -> str:
    """A new ULID for ``now``, with random entropy."""
    return new_ulid(int(now.timestamp() * 1000), os.urandom(10))


def make_ack(ref: str, status: str, now: datetime, item_expires_at: datetime | None) -> bytes:
    """The ``envelope.json`` document of an ack for envelope ``ref``."""
    expires_at = now + ACK_MIN_TTL
    if item_expires_at is not None and item_expires_at > expires_at:
        expires_at = item_expires_at
    ack = AckEnvelope(
        v=ENVELOPE_VERSION,
        id=new_id(now),
        sent_at=now,
        expires_at=expires_at,
        kind="ack",
        body=AckBody(ref=ref, status=status),  # type: ignore[arg-type]
    )
    return dump_envelope(ack)


def bot_sender(bot: object) -> SendDocument:
    """A ``SendDocument`` over a ``python-telegram-bot`` ``Bot``."""

    async def send(chat_id: str, data: bytes, filename: str) -> None:
        await bot.send_document(  # type: ignore[attr-defined]
            chat_id=chat_id, document=data, filename=filename
        )

    return send
