"""Core-client tests: submit_turn against a fake core (202/403/429) and SSE."""

from __future__ import annotations

import httpx
import pytest
from joshua_channels.core_client import CoreClient, build_core_client
from joshua_shared.contracts import Chat, Handle, TurnEvent
from joshua_shared.http import FleetClient


def _event() -> TurnEvent:
    return TurnEvent(
        channel="telegram:998877",
        chat=Chat(kind="dm", id="998877"),
        handle=Handle(type="telegram", id="998877"),
        text="hello",
        message_id="m-1",
    )


def _client(handler) -> CoreClient:
    fleet = FleetClient("http://core", "chan-tok", transport=httpx.MockTransport(handler))
    return CoreClient(fleet, busy_retry_after_s=0.0)


async def test_submit_turn_accepted_202() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        return httpx.Response(202, json={"accepted": True, "turn_id": "t-42"})

    client = _client(handler)
    ack = await client.submit_turn(_event())
    await client.aclose()

    assert ack.accepted is True
    assert ack.status == 202
    assert ack.turn_id == "t-42"
    assert seen["auth"] == "Bearer chan-tok"
    assert seen["path"] == "/v1/turns"


async def test_submit_turn_unknown_sender_403_not_retried() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(403, json={"reason": "unknown_sender"})

    client = _client(handler)
    ack = await client.submit_turn(_event())
    await client.aclose()

    assert ack.accepted is False
    assert ack.status == 403
    assert ack.reason == "unknown_sender"
    assert attempts["n"] == 1


async def test_submit_turn_429_retries_once_then_recovers() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, json={"reason": "overloaded"})
        return httpx.Response(202, json={"turn_id": "t-9"})

    client = _client(handler)
    ack = await client.submit_turn(_event())
    await client.aclose()

    assert ack.accepted is True
    assert ack.turn_id == "t-9"
    assert attempts["n"] == 2


async def test_submit_turn_429_twice_drops(caplog: pytest.LogCaptureFixture) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429, json={"reason": "overloaded"})

    client = _client(handler)
    with caplog.at_level("WARNING"):
        ack = await client.submit_turn(_event())
    await client.aclose()

    assert ack.accepted is False
    assert ack.status == 429
    assert ack.reason == "overloaded"
    assert attempts["n"] == 2
    assert any(
        isinstance(r.msg, dict) and r.msg["message"].startswith("turn dropped")
        for r in caplog.records
    )


async def test_stream_turn_yields_sse_frames() -> None:
    sse = (
        'event: delta\ndata: {"text": "hi"}\n\n'
        'event: done\ndata: {"turn_id": "t-1", "text": "hi there"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/turns/stream"
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    client = _client(handler)
    frames = [frame async for frame in client.stream_turn(_event())]
    await client.aclose()

    assert frames == [
        ("delta", {"text": "hi"}),
        ("done", {"turn_id": "t-1", "text": "hi there"}),
    ]


def test_build_core_client_needs_core_url() -> None:
    assert build_core_client({}) is None


async def test_build_core_client_from_env() -> None:
    client = build_core_client({"CORE_URL": "http://core", "JOSHUA_TOKEN_CHANNELS": "t"})
    assert client is not None
    await client.aclose()
