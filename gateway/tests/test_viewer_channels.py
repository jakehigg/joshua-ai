"""The channels client of the chat: the SSE parser, a turn, the transcript, the settings."""

from __future__ import annotations

import io
import json

import httpx
import pytest
from joshua_gateway import viewer_channels as vc

TOKEN = "viewer-token-xyz"


def _client(handler, **kwargs) -> vc.ChannelsClient:
    return vc.ChannelsClient(
        "http://channels.test", TOKEN, transport=httpx.MockTransport(handler), **kwargs
    )


def test_parser_frames_across_chunks_and_line_ends() -> None:
    parser = vc.SseParser()
    frames = parser.feed('event: delta\r\ndata: {"t": "a"}\r\n')
    assert frames == []
    frames = parser.feed("\r\n: comment\ndata: raw\n\nid: 3\n\n")
    assert frames == [("delta", {"t": "a"}), ("message", "raw")]
    assert parser.feed("event: done\ndata: 1\n") == []
    assert parser.close() == []


def test_parser_close_flushes_a_bare_cr() -> None:
    parser = vc.SseParser()
    # A trailing CR can still be followed by LF, so the frame waits.
    assert parser.feed("event: x\rdata: 1\r\r") == []
    assert parser.feed("\n") == [("x", 1)]
    parser.feed("data: 2\r")
    assert parser.close() == []


async def test_open_turn_sends_json_and_yields_frames() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, text='event: delta\ndata: {"text": "hi"}\n\nevent: done\ndata: {}\n\n'
        )

    client = _client(handler)
    stream = await client.open_turn("alex", "hello")
    frames = [frame async for frame in stream.events()]
    await client.aclose()
    assert frames == [("delta", {"text": "hi"}), ("done", {})]
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert seen["body"] == {"person": "alex", "text": "hello"}


async def test_open_turn_with_a_file_is_multipart() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["type"] = request.headers["content-type"]
        seen["body"] = request.read()
        return httpx.Response(200, text="")

    client = _client(handler)
    stream = await client.open_turn("alex", "see", ("a.png", io.BytesIO(b"PNGDATA"), "image/png"))
    _ = [frame async for frame in stream.events()]
    assert seen["type"].startswith("multipart/form-data")
    assert b"PNGDATA" in seen["body"] and b'filename="a.png"' in seen["body"]


async def test_a_refusal_is_typed_and_has_no_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"reason": "rate_limited", "detail": "slow"})

    with pytest.raises(vc.ChannelsError) as caught:
        await _client(handler).open_turn("alex", "x")
    assert (caught.value.status, caught.value.reason, caught.value.detail) == (
        429,
        "rate_limited",
        "slow",
    )
    assert TOKEN not in str(caught.value)


async def test_a_refusal_with_a_body_that_is_not_json_has_a_default_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(vc.ChannelsError) as caught:
        await _client(handler).open_turn("alex", "x")
    assert caught.value.reason == "http_500"


async def test_unreachable_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(vc.ChannelsUnavailable):
        await _client(handler).open_turn("alex", "x")
    with pytest.raises(vc.ChannelsUnavailable):
        await _client(handler).transcript("alex")


async def test_transcript_sends_its_query() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"messages": []})

    page = await _client(handler).transcript("alex", limit=5, before=9)
    assert page == {"messages": []}
    assert seen["query"] == {"person": "alex", "limit": "5", "before": "9"}


@pytest.mark.parametrize("answer", [httpx.Response(200, text="no"), httpx.Response(200, json=[1])])
async def test_transcript_with_a_bad_answer(answer: httpx.Response) -> None:
    with pytest.raises(vc.ChannelsError) as caught:
        await _client(lambda request: answer).transcript("alex")
    assert caught.value.reason == "bad_answer"


async def test_transcript_refusal() -> None:
    with pytest.raises(vc.ChannelsError) as caught:
        await _client(
            lambda request: httpx.Response(403, json={"reason": "unknown_person"})
        ).transcript("x")
    assert caught.value.reason == "unknown_person"


async def test_healthz() -> None:
    assert await _client(lambda r: httpx.Response(200, json={"ok": True})).healthz() is True
    assert await _client(lambda r: httpx.Response(200, json={"ok": False})).healthz() is False
    assert await _client(lambda r: httpx.Response(500, text="x")).healthz() is False


def test_from_env_needs_both_variables() -> None:
    assert vc.from_env({}) is None
    assert vc.from_env({"CHANNELS_URL": "http://c"}) is None
    assert vc.from_env({"JOSHUA_TOKEN_VIEWER": "t"}) is None
    assert vc.from_env({"CHANNELS_URL": "http://c", "JOSHUA_TOKEN_VIEWER": "t"}) is not None


def test_settings_defaults_and_overrides() -> None:
    default = vc.ChatSettings.from_env({})
    assert (
        default.connect_timeout_s,
        default.outbox_timeout_s,
        default.sse_keepalive_s,
        default.turn_max_bytes,
    ) == (10.0, 10.0, 20.0, 10485760)
    custom = vc.ChatSettings.from_env(
        {
            "SSE_KEEPALIVE_S": "2.5",
            "TURN_MAX_BYTES": "1000",
            "OUTBOX_TIMEOUT_S": "bad",
            "CHANNELS_CONNECT_TIMEOUT_S": "-1",
        }
    )
    assert custom.sse_keepalive_s == 2.5
    assert custom.turn_max_bytes == 1000
    assert custom.outbox_timeout_s == 10.0
    assert custom.connect_timeout_s == 10.0
