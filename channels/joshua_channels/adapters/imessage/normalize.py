"""BlueBubbles message payload → InboundMessage, plus the inbound guards.

PURE — no I/O, no clock, no config lookups. Everything here takes a plain
mapping (a webhook ``data`` object or a ``/api/v1/message/query`` row: BB emits
the same message shape for both) and returns dataclasses, so every guard is
fixture-testable without a network or an event loop.

Guard order is load-bearing:

  1. isFromMe            — our own sends echo back through the webhook. NOTE:
                           ``handle`` is null on our own GROUP messages, so this
                           must run before anything that touches the handle.
  2. tapback             — reactions ("Liked a message") arrive as real messages
                           carrying associatedMessageGuid / associatedMessageType.
  3. missing chat guid   — an ``updated-message`` (delivery/read receipt) has no
                           ``chats`` array at all; there is nothing to reply to.
  4. no content          — empty text AND no attachments. Hidden plugin-payload
                           attachments are omitted before this runs, so a link
                           preview balloon is judged on its text (the URL).

Dedupe on ``guid`` is guard 5 and lives in dedupe.py, applied at the pipeline
level (``Forwarder.submit``) because it is stateful. The staleness guard is 6
and lives in forwarder.py for the same reason (it needs a clock).
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

# Group chat guids look like ``iMessage;+;chat100000000000000001``, DMs like
# ``iMessage;-;+15551234567``. The separator is the only reliable group signal:
# ``style`` (43 group / 45 DM) is absent from some payloads, and the webhook's
# ``participants`` array is BLANKED whenever the payload exceeds ~4000 bytes.
GROUP_MARKER = ";+;"

DEFAULT_SERVICE = "iMessage"

# iMessage puts U+FFFC (OBJECT REPLACEMENT CHARACTER) in the text wherever an
# attachment sits. It is not content: a photo-only message arrives as text
# "￼", which must count as empty for guard 4.
OBJECT_REPLACEMENT = "￼"

# Messages.app splits "prose + a link" into TWO messages ~250ms apart: the text,
# then a URL balloon (balloonBundleId com.apple.messages.URLBalloonProvider)
# whose text IS the url and which drags a hidden ~20KB link-preview blob along
# as an "attachment" — transferName "<UUID>.pluginPayloadAttachment", mimeType
# null, hideAttachment true. That blob is Messages.app internal state, not
# content: nobody sent it and core cannot do anything with it.
PLUGIN_PAYLOAD_SUFFIX = ".pluginpayloadattachment"

# Drop reasons (stable strings — they show up in logs and tests).
DROP_FROM_ME = "from_me"
DROP_TAPBACK = "tapback"
DROP_NO_CHAT = "no_chat"
DROP_NO_CONTENT = "no_content"
DROP_DUPLICATE = "duplicate"
# Pipeline-level (see forwarder): a webhook replayed hours later would re-run a
# turn once the dedupe TTL has expired.
DROP_STALE = "stale"

SKIP_TOO_LARGE = "too_large"
SKIP_NO_GUID = "no_guid"
SKIP_DOWNLOAD_FAILED = "download_failed"


@dataclass(frozen=True)
class Attachment:
    """One attachment as fetched from BlueBubbles.

    ``guid`` is BB's download handle. The channels attachment pipeline turns the
    downloaded bytes into the data-volume ``Attachment`` that core sees, so this
    guid never leaves the adapter.
    """

    index: int
    filename: str
    mime_type: str | None
    size: int | None
    guid: str = ""
    skipped: bool = False
    skip_reason: str | None = None

    def skip(self, reason: str) -> Attachment:
        return replace(self, skipped=True, skip_reason=reason)


@dataclass(frozen=True)
class InboundMessage:
    """A normalized inbound iMessage, ready for the guard and the pipeline."""

    message_guid: str
    chat_guid: str
    is_group: bool
    sender_address: str
    sender_service: str
    text: str
    chat_display_name: str | None
    date_created: int
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class Rejected:
    """A message that must not be forwarded, and why."""

    reason: str
    guid: str = ""
    detail: str = ""


Normalized = InboundMessage | Rejected


# ── primitive coercions ──────────────────────────────────────────────────────


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


# ── guards (pure predicates) ─────────────────────────────────────────────────


def is_from_me(data: Mapping[str, Any]) -> bool:
    """Guard 1. Our own outbound messages echo back through the webhook.

    ``handle`` is null on our own group messages, so nothing downstream may read
    the handle before this runs.
    """
    return bool(data.get("isFromMe"))


def is_tapback(data: Mapping[str, Any]) -> bool:
    """Guard 2. Reactions arrive as ordinary messages with an association.

    ``associatedMessageType`` may arrive as an int (2000) or a string ("2000");
    0/None means "not an association". A non-numeric non-empty string is
    treated as an association too (BB has shipped symbolic values).
    """
    if data.get("associatedMessageGuid"):
        return True
    raw = data.get("associatedMessageType")
    if raw is None or raw == "":
        return False
    parsed = _as_int(raw, default=None)
    if parsed is None:
        return True  # non-numeric marker → still an association
    return parsed != 0


def first_chat(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Guard 3 helper. ``chats`` is absent on updated-message payloads."""
    chats = data.get("chats")
    if not isinstance(chats, Sequence) or isinstance(chats, str | bytes):
        return None
    for chat in chats:
        chat_map = _mapping(chat)
        if _as_str(chat_map.get("guid")):
            return chat_map
    return None


def clean_text(raw: Any) -> str:
    """Strip iMessage's attachment placeholder and surrounding whitespace."""
    return _as_str(raw).replace(OBJECT_REPLACEMENT, "").strip()


def has_content(text: str, attachments: Sequence[Attachment]) -> bool:
    """Guard 4. Empty text with no attachments is nothing to act on."""
    return bool(text) or bool(attachments)


def is_group_guid(chat_guid: str) -> bool:
    return GROUP_MARKER in chat_guid


def normalize_address(address: Any) -> str:
    """Emails lowercased, phone numbers verbatim.

    Apple hands back mixed-case emails for the same person; E.164 numbers are
    already canonical and must not be touched.
    """
    value = _as_str(address).strip()
    return value.lower() if "@" in value else value


def sender_of(data: Mapping[str, Any], chat: Mapping[str, Any]) -> tuple[str, str]:
    """(address, service). Falls back to the chat identifier for a DM whose
    handle is missing; a group with no handle yields an empty address (the
    from-me guard has already removed the case where that is expected)."""
    handle = _mapping(data.get("handle"))
    address = normalize_address(handle.get("address"))
    if not address and not is_group_guid(_as_str(chat.get("guid"))):
        address = normalize_address(chat.get("chatIdentifier") or data.get("chatIdentifier"))
    service = _as_str(handle.get("service")) or _as_str(chat.get("service"))
    return address, service or DEFAULT_SERVICE


def safe_filename(raw: Any, index: int) -> str:
    """Basename only — a transferName is attacker-influenced free text and ends
    up as a stored filename."""
    name = posixpath.basename(_as_str(raw).replace("\\", "/")).strip()
    return name or f"attachment-{index}"


def is_hidden_attachment(entry: Mapping[str, Any]) -> bool:
    """True for attachments Messages.app hides from the human reader.

    Two signals, either is enough: ``hideAttachment`` (BB passes Apple's flag
    through) and the ``.pluginPayloadAttachment`` transferName that a link
    preview / rich-link balloon carries.
    """
    if entry.get("hideAttachment"):
        return True
    name = _as_str(entry.get("transferName")).strip().lower()
    return name.endswith(PLUGIN_PAYLOAD_SUFFIX)


def normalize_attachments(raw: Any, *, max_bytes: int) -> tuple[Attachment, ...]:
    """Attachment metadata, with the size-based skip decision already applied.

    Download failures are the forwarder's business (it owns the I/O); the
    oversize decision is pure and belongs here.

    Hidden plugin-payload attachments are OMITTED, not skipped: ``index`` binds
    ``attachments[i]`` to the downloaded blob, so a hidden blob must not occupy
    an index at all. Indexes are assigned over what survives, contiguously
    from 0.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    out: list[Attachment] = []
    for item in raw:
        entry = _mapping(item)
        if is_hidden_attachment(entry):
            continue
        index = len(out)
        size = _as_int(entry.get("totalBytes"), default=None)
        att = Attachment(
            index=index,
            filename=safe_filename(entry.get("transferName"), index),
            mime_type=_as_str(entry.get("mimeType")) or None,
            size=size,
            guid=_as_str(entry.get("guid")),
        )
        if not att.guid:
            att = att.skip(SKIP_NO_GUID)
        elif size is not None and size > max_bytes:
            att = att.skip(SKIP_TOO_LARGE)
        out.append(att)
    return tuple(out)


# ── the pipeline's pure half ─────────────────────────────────────────────────


def normalize_message(data: Any, *, max_attachment_bytes: int) -> Normalized:
    """Run guards 1-4 and build the InboundMessage. Never raises."""
    payload = _mapping(data)
    guid = _as_str(payload.get("guid"))

    if is_from_me(payload):
        return Rejected(DROP_FROM_ME, guid)
    if is_tapback(payload):
        return Rejected(DROP_TAPBACK, guid, detail=str(payload.get("associatedMessageType")))

    chat = first_chat(payload)
    if chat is None:
        return Rejected(DROP_NO_CHAT, guid, detail="no chats[].guid in payload")

    chat_guid = _as_str(chat.get("guid"))
    text = clean_text(payload.get("text"))
    attachments = normalize_attachments(payload.get("attachments"), max_bytes=max_attachment_bytes)
    if not has_content(text, attachments):
        return Rejected(DROP_NO_CONTENT, guid)

    address, service = sender_of(payload, chat)
    display_name = _as_str(chat.get("displayName")) or None

    return InboundMessage(
        message_guid=guid,
        chat_guid=chat_guid,
        is_group=is_group_guid(chat_guid),
        sender_address=address,
        sender_service=service,
        text=text,
        chat_display_name=display_name,
        date_created=_as_int(payload.get("dateCreated"), 0) or 0,
        attachments=attachments,
    )


def unwrap_webhook(body: Any) -> tuple[str, Mapping[str, Any]]:
    """``{"type": ..., "data": {...}}`` → (type, data). Tolerant of junk."""
    envelope = _mapping(body)
    return _as_str(envelope.get("type")), _mapping(envelope.get("data"))
