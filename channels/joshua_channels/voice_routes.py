"""The voice channel's inbound routes.

``POST /v1/voice/chat/completions`` takes one spoken turn in the OpenAI
chat-completions shape, runs the same guard every channel runs, and streams
Joshua's reply back as OpenAI chunks. ``GET /v1/models`` reports the one model
id, because an OpenAI client asks for the list before it sends anything.

The route speaks OpenAI so that the voice front end is free. Any client that can
point a base URL at Joshua works: a Pipecat bot, a phone app, a shell with
``curl``. Joshua adds two fields to the body, ``speaker`` and
``speaker_confidence``, and ignores everything a client sends that it does not
need (``model``, ``temperature``, and the rest).

Two markers ride in the reply text and are part of this contract: ``[DONE]``
asks the front end to end the call, and ``[IGNORE]`` asks it to speak nothing and
stay open. They are text, and the front end removes them before it speaks. They
are not the SSE terminator ``data: [DONE]``, which ends the HTTP body.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from joshua_shared.config import VoiceChannel
from joshua_shared.contracts import Chat, Handle, TurnEvent
from joshua_shared.fleet_auth import authenticate, load_fleet_tokens
from joshua_shared.log import get_logger

from joshua_channels.adapters import voice as voice_adapter
from joshua_channels.adapters.voice import CHANNEL_TYPE, IdentityHold, Speaker
from joshua_channels.deliver import ChannelsContext

logger = get_logger("channels.voice")

CHAT_COMPLETIONS_PATH = "/v1/voice/chat/completions"
MODELS_PATH = "/v1/models"

# The address a refusal records when the front end named no speaker at all.
UNNAMED_SPEAKER = "unnamed"

_SSE_MEDIA_TYPE = "text/event-stream"
_SSE_TERMINATOR = "data: [DONE]\n\n"


def _context(request: Request) -> ChannelsContext:
    return request.app.state.ctx


def _voice(ctx: ChannelsContext) -> VoiceChannel | None:
    return ctx.settings.channels.voice


def _auth(request: Request, voice: VoiceChannel) -> JSONResponse | None:
    """401 for a token that matches nothing, 403 for an identity that may not speak."""
    status, _ = authenticate(
        request.headers.get("authorization"),
        load_fleet_tokens(),
        allowed=list(voice.allowed_callers),
    )
    if status == 401:
        return JSONResponse({"reason": "unauthorized"}, status_code=401)
    if status == 403:
        return JSONResponse({"reason": "forbidden"}, status_code=403)
    return None


def _sse(data: dict[str, Any]) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _one_reply(text: str, *, stream: bool) -> StreamingResponse | JSONResponse:
    """Answer with one whole reply, in whichever shape the client asked for."""
    completion_id = voice_adapter.completion_id()
    created = int(time.time())
    if not stream:
        return JSONResponse(voice_adapter.completion(completion_id, created, text))

    async def frames() -> AsyncIterator[str]:
        yield _sse(voice_adapter.chunk(completion_id, created, role="assistant", content=text))
        yield _sse(voice_adapter.chunk(completion_id, created, finish_reason="stop"))
        yield _SSE_TERMINATOR

    return StreamingResponse(frames(), media_type=_SSE_MEDIA_TYPE)


def _turn_event(voice: VoiceChannel, speaker: Speaker, text: str) -> TurnEvent:
    channel, chat_id = voice_adapter.conversation_key(voice, speaker)
    return TurnEvent(
        channel=channel,
        chat=Chat(kind="dm", id=chat_id),
        handle=Handle(type=CHANNEL_TYPE, id=speaker.handle),
        text=text,
        framing=voice_adapter.framing_for(speaker),
    )


def build_voice_router(identities: IdentityHold) -> APIRouter:
    """The voice channel routes. ``identities`` is the per-device identity hold."""
    router = APIRouter()

    @router.get(MODELS_PATH)
    async def get_models(request: Request):  # type: ignore[no-untyped-def]
        ctx = _context(request)
        voice = _voice(ctx)
        if voice is None:
            return JSONResponse({"reason": "not_found"}, status_code=404)
        denied = _auth(request, voice)
        if denied is not None:
            return denied
        return JSONResponse(voice_adapter.models_list(int(time.time())))

    @router.post(CHAT_COMPLETIONS_PATH)
    async def post_voice_turn(request: Request):  # type: ignore[no-untyped-def]
        ctx = _context(request)
        voice = _voice(ctx)
        if voice is None:
            return JSONResponse({"reason": "not_found"}, status_code=404)
        denied = _auth(request, voice)
        if denied is not None:
            return denied

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a bad body is a 400, not a 500
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"reason": "bad_request"}, status_code=400)

        stream = bool(body.get("stream", False))
        text = voice_adapter.last_user_text(body.get("messages"))
        if not text:
            return JSONResponse({"reason": "bad_request"}, status_code=400)
        if len(text) > voice.max_text_chars:
            text = text[: voice.max_text_chars]

        device = voice_adapter.device_of(body.get("user"))
        cfg = ctx.settings
        speaker = voice_adapter.identify(
            cfg,
            voice,
            identities,
            device=device,
            speaker=voice_adapter.speaker_name(body.get("speaker")),
            confidence=voice_adapter.confidence_of(body.get("speaker_confidence")),
        )
        channel, chat_id = voice_adapter.conversation_key(voice, speaker)

        if ctx.guard is not None:
            # The adapter, not the guard, decides who spoke: a name with a score
            # below ``min_confidence`` must not go back through the roster
            # lookup. An unidentified speaker is refused here, and the refusal
            # still reaches the counters and ``/admin/guard/recent``.
            if speaker.person is None:
                verdict = ctx.guard.refuse(
                    channel_type=CHANNEL_TYPE,
                    address=speaker.claimed or UNNAMED_SPEAKER,
                    chat_id=chat_id,
                )
            else:
                verdict = ctx.guard.check(
                    channel_type=CHANNEL_TYPE,
                    sender_handle=speaker.handle,
                    chat_id=chat_id,
                    chat_kind="dm",
                    text_len=len(text),
                    attachment_bytes=0,
                )
            if not verdict.allowed:
                logger.warning(
                    {
                        "message": "voice turn refused",
                        "device": device,
                        "reason": verdict.reason,
                        "held": speaker.held,
                    }
                )
                reply = (
                    voice_adapter.UNKNOWN_SPEAKER_REPLY
                    if voice.unknown_sender == "reply"
                    else voice_adapter.IGNORE_REPLY
                )
                return _one_reply(reply, stream=stream)

        if ctx.core_client is None:
            return JSONResponse({"reason": "core_unavailable"}, status_code=503)

        logger.info(
            {
                "message": "voice turn",
                "device": device,
                "channel": channel,
                "person": speaker.person.id if speaker.person else None,
                "held": speaker.held,
            }
        )
        event = _turn_event(voice, speaker, text)
        if not stream:
            return await _collect(ctx, event)
        return StreamingResponse(_stream(ctx, event), media_type=_SSE_MEDIA_TYPE)

    return router


def _frames(ctx: ChannelsContext, event: TurnEvent) -> AsyncIterator[tuple[str, dict]]:
    """Core's SSE frames for one turn."""
    assert ctx.core_client is not None
    return ctx.core_client.stream_turn(event)


async def _stream(ctx: ChannelsContext, event: TurnEvent) -> AsyncIterator[str]:
    """Re-wrap core's turn stream as OpenAI chunks.

    Each ``delta`` becomes one content chunk, so the front end can start to speak
    before the turn ends. The ``done`` frame closes the reply; its text is sent
    only when no delta arrived, which is what a backend that does not stream
    produces. An ``error`` frame becomes one short spoken sentence, and the cause
    goes to the log.
    """
    completion_id = voice_adapter.completion_id()
    created = int(time.time())
    yield _sse(voice_adapter.chunk(completion_id, created, role="assistant"))
    streamed = False
    try:
        async for name, data in _frames(ctx, event):
            if name == "delta":
                text = str(data.get("text") or "")
                if text:
                    streamed = True
                    yield _sse(voice_adapter.chunk(completion_id, created, content=text))
            elif name == "done":
                text = str(data.get("text") or "")
                if not streamed and text:
                    yield _sse(voice_adapter.chunk(completion_id, created, content=text))
                break
            elif name == "error":
                logger.error(
                    {
                        "message": "voice turn failed",
                        "channel": event.channel,
                        "error": str(data.get("message") or ""),
                    }
                )
                if not streamed:
                    yield _sse(
                        voice_adapter.chunk(
                            completion_id, created, content=voice_adapter.TURN_FAILED_REPLY
                        )
                    )
                break
    except Exception as exc:  # noqa: BLE001 — the caller is a live call; answer it
        logger.error(
            {"message": "voice stream failed", "channel": event.channel, "error": str(exc)}
        )
        if not streamed:
            yield _sse(
                voice_adapter.chunk(completion_id, created, content=voice_adapter.TURN_FAILED_REPLY)
            )
    yield _sse(voice_adapter.chunk(completion_id, created, finish_reason="stop"))
    yield _SSE_TERMINATOR


async def _collect(ctx: ChannelsContext, event: TurnEvent) -> JSONResponse:
    """Run the turn and answer with one ``chat.completion`` object."""
    parts: list[str] = []
    final = ""
    failed = False
    try:
        async for name, data in _frames(ctx, event):
            if name == "delta":
                parts.append(str(data.get("text") or ""))
            elif name == "done":
                final = str(data.get("text") or "")
                break
            elif name == "error":
                logger.error(
                    {
                        "message": "voice turn failed",
                        "channel": event.channel,
                        "error": str(data.get("message") or ""),
                    }
                )
                failed = True
                break
    except Exception as exc:  # noqa: BLE001 — the caller is a live call; answer it
        logger.error({"message": "voice turn failed", "channel": event.channel, "error": str(exc)})
        failed = True
    text = final or "".join(parts)
    if failed and not text:
        text = voice_adapter.TURN_FAILED_REPLY
    return JSONResponse(
        voice_adapter.completion(voice_adapter.completion_id(), int(time.time()), text)
    )
