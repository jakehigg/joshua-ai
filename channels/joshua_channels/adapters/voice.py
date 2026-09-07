"""The voice channel: speaker identity, conversation keys, and the OpenAI shapes.

A voice front end (a Pipecat bot, a phone app, anything that speaks the OpenAI
chat-completions protocol) sends one spoken turn to
``POST /v1/voice/chat/completions``. This module holds the parts of that route
that have no HTTP in them: it reads the request body, decides who spoke, picks
the conversation the turn belongs to, and builds the response objects.

Two things make voice different from a typed channel:

- **The microphone is shared.** The front end identifies the speaker and sends a
  score with the name. Below ``min_confidence`` the score is not trusted, so the
  device keeps the last identity it was sure about for ``identity_hold_s``
  seconds. Nothing here ever adds a person.
- **A person moves.** With ``thread: person`` an identified member keeps one
  conversation as they walk from room to room, so the channel is
  ``voice:threads`` and the device rides along as framing. A guest stays on
  ``voice:<device>``, because a guest device is a place, not a person.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any
from uuid import uuid4

from joshua_shared.config import JoshuaConfig, Person, VoiceChannel

CHANNEL_TYPE = "voice"

# The channel that carries a conversation which follows a person between
# devices. It is a fixed name, so a front end never needs to know about it.
ROAMING_CHANNEL = "voice:threads"
ROAMING_CHAT_ID = "threads"

# The device a turn is attributed to when the front end names none.
DEFAULT_DEVICE = "default"

# The model id ``GET /v1/models`` reports. A client must send a model, and the
# route ignores what it sends, so one id is enough.
MODEL_ID = "joshua"

# The reply text for a speaker Joshua cannot identify. ``[DONE]`` ends the call,
# ``[IGNORE]`` tells the front end to speak nothing and stay open.
UNKNOWN_SPEAKER_REPLY = "I do not know your voice. [DONE]"
IGNORE_REPLY = "[IGNORE]"

# The reply text for a turn that failed. The cause goes to the log, never to the
# speaker.
TURN_FAILED_REPLY = "Sorry, something went wrong on my end. Please say that again."

_PERSON_PREFIX = "person:"


@dataclass(frozen=True)
class Speaker:
    """Who the route decided is speaking, and where.

    ``person`` is None for a speaker that Joshua does not trust: no roster entry
    claims the name, or the score was too low and the device holds nothing.
    ``handle`` is the identified person's voice handle, and empty when there is
    no person. ``claimed`` is the raw name the front end sent, whether or not it
    was trusted; a refusal records it, so an operator reads who spoke and can add
    them. ``held`` is True when the identity came from the hold and not from this
    turn's score.
    """

    device: str
    person: Person | None
    handle: str
    claimed: str = ""
    confidence: float | None = None
    held: bool = False


class IdentityHold:
    """The last identity each device was sure about, and when it expires.

    A short spoken turn ("yes", "stop") gives the front end too little audio to
    score well. Without a hold, every such turn would fall to the unknown-speaker
    policy in the middle of a conversation. The hold is memory only: it is lost
    on a restart, which costs one turn of identification.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # device -> (person id, expiry)
        self._held: dict[str, tuple[str, float]] = {}
        self._lock = Lock()

    def remember(self, device: str, person_id: str, hold_s: int) -> None:
        """Hold ``person_id`` for ``device``. A hold of zero seconds holds nothing."""
        if hold_s <= 0:
            return
        with self._lock:
            self._held[device] = (person_id, self._clock() + hold_s)

    def held(self, device: str) -> str | None:
        """The person id this device still holds, or None."""
        with self._lock:
            entry = self._held.get(device)
            if entry is None:
                return None
            person_id, expires_at = entry
            if self._clock() >= expires_at:
                del self._held[device]
                return None
            return person_id

    def forget(self, device: str) -> None:
        """Drop the hold for one device."""
        with self._lock:
            self._held.pop(device, None)

    def count(self) -> int:
        """How many devices hold a live identity. For ``/readyz``."""
        now = self._clock()
        with self._lock:
            return sum(1 for _, expires_at in self._held.values() if now < expires_at)


# --- the request ------------------------------------------------------------


def device_of(user: Any) -> str:
    """The device id in an OpenAI ``user`` field.

    A front end sends either the channel (``voice:office``) or the bare device
    name (``office``). Both name the same device. An empty or unusable value
    gives ``DEFAULT_DEVICE``, so a client that sends no ``user`` still works.
    """
    if not isinstance(user, str):
        return DEFAULT_DEVICE
    value = user.strip()
    if value.startswith(f"{CHANNEL_TYPE}:"):
        value = value[len(CHANNEL_TYPE) + 1 :]
    value = value.strip()
    return value or DEFAULT_DEVICE


def speaker_name(speaker: Any) -> str:
    """The speaker name in a ``speaker`` field, with any ``person:`` prefix removed."""
    if not isinstance(speaker, str):
        return ""
    value = speaker.strip()
    if value.startswith(_PERSON_PREFIX):
        value = value[len(_PERSON_PREFIX) :]
    return value.strip()


def confidence_of(value: Any) -> float | None:
    """The speaker score as a float, or None when the front end sent none."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def last_user_text(messages: Any) -> str:
    """The text of the last ``user`` message in an OpenAI message list.

    A voice turn is one utterance. The front end sends the conversation it holds,
    and Joshua holds its own, so only the newest utterance is read. Content that
    arrives as a list of parts is joined from the ``text`` parts.
    """
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        return _content_text(message.get("content"))
    return ""


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "".join(parts).strip()
    return ""


def identify(
    cfg: JoshuaConfig,
    voice: VoiceChannel,
    hold: IdentityHold,
    *,
    device: str,
    speaker: str,
    confidence: float | None,
) -> Speaker:
    """Decide who is speaking on ``device``.

    A name with a score at or above ``min_confidence`` (or with no score at all)
    is looked up in ``people[].handles.voice``. A confident match refreshes the
    device's hold. A name Joshua does not trust, or no name at all, falls back to
    the identity the device still holds. Nothing here creates a person.
    """
    person: Person | None = None
    trusted = confidence is None or confidence >= voice.min_confidence
    if speaker and trusted:
        person = cfg.people_by_handle(CHANNEL_TYPE, speaker)

    if person is not None:
        hold.remember(device, person.id, voice.identity_hold_s)
        return Speaker(
            device=device,
            person=person,
            handle=speaker,
            claimed=speaker,
            confidence=confidence,
        )

    held_id = hold.held(device)
    if held_id is not None:
        held_person = cfg.person(held_id)
        if held_person is not None:
            handle = held_person.handles.get(CHANNEL_TYPE) or held_person.id
            return Speaker(
                device=device,
                person=held_person,
                handle=handle,
                claimed=speaker,
                confidence=confidence,
                held=True,
            )
        hold.forget(device)

    # No person, so no handle. A name Joshua does not trust must never reach the
    # roster lookup again: that would turn an untrusted claim into an identity.
    return Speaker(device=device, person=None, handle="", claimed=speaker, confidence=confidence)


def conversation_key(voice: VoiceChannel, speaker: Speaker) -> tuple[str, str]:
    """The ``(channel, chat_id)`` a turn belongs to.

    A member on a ``thread: person`` channel keeps one conversation wherever they
    speak. Everybody else stays on the device, so two guests in two rooms do not
    share a conversation.
    """
    if voice.thread == "person" and speaker.person is not None and speaker.person.role == "member":
        return ROAMING_CHANNEL, ROAMING_CHAT_ID
    return f"{CHANNEL_TYPE}:{speaker.device}", speaker.device


def framing_for(speaker: Speaker) -> str:
    """The line that tells the agent where the turn was spoken.

    The device is the room. A conversation that follows a person carries no room
    in its channel id, so the agent reads the room here. A tool that acts on a
    device (a timer, an announcement, a screen) takes the room from this line
    when the person does not name one.
    """
    return (
        f"This turn was spoken on the `{speaker.device}` voice device. When a tool "
        "asks which device or room to act on, and the person did not name one, use "
        "this device."
    )


# --- the response -----------------------------------------------------------


def completion_id() -> str:
    """An OpenAI-shaped completion id."""
    return f"chatcmpl-{uuid4().hex[:24]}"


def chunk(
    completion: str,
    created: int,
    *,
    role: str | None = None,
    content: str | None = None,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    """One ``chat.completion.chunk`` object."""
    delta: dict[str, Any] = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    return {
        "id": completion,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def completion(completion_id_: str, created: int, text: str) -> dict[str, Any]:
    """One ``chat.completion`` object, for a request with ``stream: false``."""
    return {
        "id": completion_id_,
        "object": "chat.completion",
        "created": created,
        "model": MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }


def models_list(created: int) -> dict[str, Any]:
    """The body of ``GET /v1/models``: the one model this route answers for."""
    return {
        "object": "list",
        "data": [{"id": MODEL_ID, "object": "model", "created": created, "owned_by": "joshua"}],
    }
