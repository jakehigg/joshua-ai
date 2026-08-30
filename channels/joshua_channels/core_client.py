"""The channels → core client for inbound turns.

Every adapter normalizes a platform message into a ``TurnEvent`` and submits it
here. ``submit_turn`` posts to ``POST /v1/turns`` and expects 202; it never
retries an unknown sender (403) and retries an overloaded core (429) once before
it drops the turn. ``stream_turn`` runs a turn inline and yields core's SSE
events; tests and the later voice path use it.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping

from joshua_shared.contracts import TurnAck, TurnEvent
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger

logger = get_logger("channels.core_client")

CORE_URL_ENV = "CORE_URL"
CHANNELS_TOKEN_ENV = "JOSHUA_TOKEN_CHANNELS"
TURNS_PATH = "/v1/turns"
STREAM_PATH = "/v1/turns/stream"

# One retry after core answers 429, then the turn is dropped.
BUSY_RETRY_AFTER_S = 2.0


class CoreClient:
    """Submit turns to core over the fleet channel."""

    def __init__(
        self, client: FleetClient, *, busy_retry_after_s: float = BUSY_RETRY_AFTER_S
    ) -> None:
        self._client = client
        self._busy_retry_after = busy_retry_after_s

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def submit_turn(self, event: TurnEvent) -> TurnAck:
        """Post one turn to ``/v1/turns`` and return core's acknowledgement.

        A 202 is accepted. A 403 (unknown sender) is logged and not retried. A 429
        is retried once after ``busy_retry_after_s``; a second 429 drops the turn
        with an audit line.
        """
        body = event.model_dump(mode="json")
        response = await self._client.post_json(TURNS_PATH, json=body)
        if response.status_code == 429:
            logger.warning(
                {"message": "core overloaded; retrying turn once", "channel": event.channel}
            )
            await asyncio.sleep(self._busy_retry_after)
            response = await self._client.post_json(TURNS_PATH, json=body)
            if response.status_code == 429:
                logger.warning(
                    {
                        "message": "turn dropped: core overloaded after retry",
                        "channel": event.channel,
                        "message_id": event.message_id,
                    }
                )
                return TurnAck(accepted=False, status=429, reason="overloaded")
        return self._ack(event, response.status_code, _json(response))

    def _ack(self, event: TurnEvent, status: int, data: dict) -> TurnAck:
        if status == 202:
            return TurnAck(accepted=True, status=202, turn_id=data.get("turn_id"))
        if status == 403:
            logger.warning(
                {
                    "message": "turn rejected: unknown sender",
                    "channel": event.channel,
                    "handle_type": event.handle.type if event.handle else None,
                }
            )
            return TurnAck(
                accepted=False, status=403, reason=data.get("reason") or "unknown_sender"
            )
        logger.warning({"message": "turn not accepted", "channel": event.channel, "status": status})
        return TurnAck(accepted=False, status=status, reason=data.get("reason"))

    async def stream_turn(self, event: TurnEvent) -> AsyncIterator[tuple[str, dict]]:
        """Run a turn inline and yield ``(event_name, data)`` for each SSE frame."""
        body = event.model_dump(mode="json")
        async with self._client.stream("POST", STREAM_PATH, json=body) as response:
            async for frame in _iter_sse(response):
                yield frame


def _json(response) -> dict:  # type: ignore[no-untyped-def]
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _iter_sse(response) -> AsyncIterator[tuple[str, dict]]:  # type: ignore[no-untyped-def]
    """Yield ``(event, data)`` pairs from a text/event-stream response."""
    name: str | None = None
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if name is not None:
                yield name, _parse_data(data_lines)
            name, data_lines = None, []
            continue
        field, _, value = line.partition(":")
        if field == "event":
            name = value.strip()
        elif field == "data":
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if name is not None:
        yield name, _parse_data(data_lines)


def _parse_data(lines: list[str]) -> dict:
    raw = "\n".join(lines)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"text": raw}
    return data if isinstance(data, dict) else {"value": data}


def build_core_client(
    env: Mapping[str, str] | None = None, *, timeout: float = 15.0
) -> CoreClient | None:
    """Build a ``CoreClient`` from the environment, or None when ``CORE_URL`` is unset.

    Reads ``CORE_URL`` (core's base URL) and ``JOSHUA_TOKEN_CHANNELS`` (the bearer
    channels sends). A None return lets channels boot in deliver-only mode before
    any adapter needs core.
    """
    source = os.environ if env is None else env
    base_url = source.get(CORE_URL_ENV)
    if not base_url:
        logger.warning({"message": f"{CORE_URL_ENV} is not set; core client disabled"})
        return None
    client = FleetClient(base_url, source.get(CHANNELS_TOKEN_ENV, ""), timeout=timeout)
    return CoreClient(client)
