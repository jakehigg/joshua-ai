"""Shared API shapes for the channels → core contract.

Channels normalizes every platform message into a ``TurnEvent`` and posts it to
core. Both containers import these models, so the wire shape has one definition.
Core never trusts ``framing`` from user input; only the scheduler and ops set it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

HandleType = Literal["telegram", "imessage", "webhook", "voice", "cli"]
ChatKind = Literal["dm", "group"]
TurnKind = Literal["message", "event"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Handle(_Model):
    """The verified sender, stamped by channels. ``id`` is the platform handle."""

    type: HandleType
    id: str


class Chat(_Model):
    """The conversation surface. ``kind`` picks per-person or shared (group)."""

    kind: ChatKind
    id: str
    title: str | None = None


class Attachment(_Model):
    """One inbound file. ``path`` is files-MCP relative: ``attachments/YYYY/MM/<file>``.

    ``name`` is the stored filename. ``original_name`` is what the platform called
    the file, so the agent can name it back to the person.
    """

    path: str
    mime: str
    name: str | None = None
    original_name: str | None = None


class EventOptions(_Model):
    """Options for a ``kind == "event"`` turn.

    ``verbatim`` makes core deliver a channel-addressed event's ``text`` straight
    to the channel with no model call.
    """

    verbatim: bool = False


class TurnEvent(_Model):
    """One normalized turn from any channel.

    ``handle`` is None only for ``kind == "event"``. ``message_id`` is the
    platform id and the idempotency key. ``event_type`` and ``payload`` carry an
    event (``kind == "event"``); core routes those.
    """

    channel: str
    chat: Chat
    handle: Handle | None = None
    kind: TurnKind = "message"
    text: str = ""
    attachments: list[Attachment] = []
    reply_to: str | None = None
    message_id: str | None = None
    event_type: str | None = None
    payload: dict | None = None
    options: EventOptions | None = None
    framing: str | None = None


class TurnAck(_Model):
    """Core's answer to a submitted turn, as the channels client records it.

    ``accepted`` is True only for a 202. ``status`` is the HTTP status the client
    saw. ``turn_id`` is set on a 202. ``reason`` names why a turn was not accepted
    (``unknown_sender``, ``overloaded``, ``duplicate``, or core's own reason).
    """

    accepted: bool
    status: int
    turn_id: str | None = None
    reason: str | None = None


class DeliverRequest(_Model):
    """Core's outbound reply. Channels resolves ``channel`` to an adapter.

    ``channel`` is a channel id, a logical ref, or a destination name.
    ``attachments`` are files-MCP relative paths (``attachments/YYYY/MM/<file>``
    or ``shared/…``); channels maps each to a data-volume path.
    """

    channel: str
    text: str = ""
    attachments: list[str] = []


class ResolveResponse(_Model):
    """The concrete channel and chat for a destination name or ref.

    ``channel_id`` and ``chat_kind`` are flat aliases of ``channel`` and
    ``chat.kind`` that core's channels resolver reads.
    """

    channel: str
    chat: Chat
    channel_id: str
    chat_kind: ChatKind
