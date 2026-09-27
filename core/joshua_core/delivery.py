"""Core's one way to say something: POST a reply to channels.

``Deliverer.deliver`` sends ``{"channel", "text", "attachments"}`` to
``POST {CHANNELS_URL}/v1/deliver`` with the core fleet token. Core never
interprets the target — channels resolves a channel id, a logical ref, or a
destination name. Core also never splits long text; channels owns platform
limits.

``is_ignore`` is the agent's silence sentinel and stays in core: a reply that is
only ``[IGNORE]`` (or empty) means "say nothing", so the turn drops before it
reaches this client.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence

import httpx
from joshua_shared import http
from joshua_shared.config import VoiceChannel
from joshua_shared.contracts import DeliverRequest
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger

logger = get_logger("delivery")

DELIVER_PATH = "/v1/deliver"
CHANNELS_URL_ENV = "CHANNELS_URL"
CORE_TOKEN_ENV = "JOSHUA_TOKEN_CORE"
DRY_RUN_ENV = "DELIVER_DRY_RUN"

IGNORE_MARKER = "[IGNORE]"

VOICE_CHANNEL_TYPE = "voice"


def voice_delivery_target(
    *,
    channel_type: str,
    conversation_id: str,
    person_id: str | None,
    target: str,
    voice: VoiceChannel | None,
) -> str | None:
    """Where a reply to ``target`` actually goes, once a voice conversation is
    handled.

    A voice channel has no outbound send: the voice adapter only answers the
    live HTTP turn, so a scheduled task's reply or an event's reply, which
    both arrive here later, has nowhere to go on ``voice:`` itself. A
    non-voice channel is unaffected and this returns ``target`` unchanged.

    For a voice conversation with a ``person_id`` and ``channels.voice.
    deliver_via`` set, the reply reroutes to that channel type's DM for the
    person instead, and this logs one INFO line naming the conversation. A
    device thread (no person) or an unset ``deliver_via`` delivers nothing:
    this logs one WARNING naming the conversation and the reason, and returns
    None so the caller settles the task or event exactly as a failed
    delivery is settled today.
    """
    if channel_type != VOICE_CHANNEL_TYPE:
        return target
    if person_id is not None and voice is not None and voice.deliver_via is not None:
        logger.info(
            {
                "message": "rerouted voice delivery",
                "conversation_id": conversation_id,
                "deliver_via": voice.deliver_via,
            }
        )
        return f"{voice.deliver_via}:dm:{person_id}"
    reason = "conversation has no person" if person_id is None else "deliver_via is not set"
    logger.warning(
        {
            "message": "voice delivery dropped",
            "conversation_id": conversation_id,
            "reason": reason,
        }
    )
    return None


def is_ignore(text: str) -> bool:
    """Return True when the agent reply means "say nothing".

    The agent emits ``[IGNORE]`` alone to stay silent. A blank reply is also
    silence. Core drops the turn and calls no channel.
    """
    stripped = text.strip()
    return not stripped or stripped == IGNORE_MARKER


def dry_run_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return True when ``DELIVER_DRY_RUN=1`` selects the shadow (silent) mode."""
    source = os.environ if env is None else env
    return source.get(DRY_RUN_ENV, "").strip() == "1"


class Deliverer:
    """Deliver a reply to channels, with retry on 5xx and connection errors."""

    def __init__(self, client: FleetClient, *, dry_run: bool = False) -> None:
        self._client = client
        self._dry_run = dry_run

    async def deliver(self, target: str, text: str, attachments: Sequence[str] = ()) -> bool:
        """POST one reply to channels. Return True on delivery, else False.

        ``target`` passes through unchanged. ``200`` is success. ``404`` (unknown
        channel) fails without retry. A ``5xx`` or a connection error retries up
        to three attempts, then fails. In dry-run mode nothing is sent and the
        call returns True.
        """
        if self._dry_run:
            logger.info({"message": "deliver (dry-run)", "target": target, "chars": len(text)})
            return True

        body = DeliverRequest(channel=target, text=text, attachments=list(attachments)).model_dump(
            mode="json"
        )
        for attempt in range(1, http.MAX_ATTEMPTS + 1):
            try:
                response = await self._client.post_json(DELIVER_PATH, json=body)
            except httpx.ConnectError:
                # FleetClient already retried the connection MAX_ATTEMPTS times.
                logger.warning({"message": "deliver failed: connection error", "target": target})
                return False

            status = response.status_code
            if status == 200:
                logger.info({"message": "delivered", "target": target, "chars": len(text)})
                return True
            if status == 404:
                logger.warning({"message": "deliver failed: unknown channel", "target": target})
                return False
            if status < 500:
                logger.warning({"message": "deliver failed", "target": target, "status": status})
                return False

            logger.warning(
                {
                    "message": "deliver failed",
                    "target": target,
                    "status": status,
                    "attempt": attempt,
                }
            )
            if attempt < http.MAX_ATTEMPTS:
                await asyncio.sleep(http.BACKOFF_BASE_S * 2 ** (attempt - 1))
        return False


def build_deliverer(env: Mapping[str, str] | None = None, *, timeout: float = 15.0) -> Deliverer:
    """Build a ``Deliverer`` from the environment.

    Reads ``CHANNELS_URL`` (the channels base URL), ``JOSHUA_TOKEN_CORE`` (the
    bearer core sends), and ``DELIVER_DRY_RUN``. Raises ``RuntimeError`` when
    ``CHANNELS_URL`` is not set.
    """
    source = os.environ if env is None else env
    base_url = source.get(CHANNELS_URL_ENV)
    if not base_url:
        raise RuntimeError(f"{CHANNELS_URL_ENV} is not set")
    client = FleetClient(base_url, source.get(CORE_TOKEN_ENV, ""), timeout=timeout)
    return Deliverer(client, dry_run=dry_run_enabled(source))
