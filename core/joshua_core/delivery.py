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
from joshua_shared.contracts import DeliverRequest
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger

logger = get_logger("delivery")

DELIVER_PATH = "/v1/deliver"
CHANNELS_URL_ENV = "CHANNELS_URL"
CORE_TOKEN_ENV = "JOSHUA_TOKEN_CORE"
DRY_RUN_ENV = "DELIVER_DRY_RUN"

IGNORE_MARKER = "[IGNORE]"


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
