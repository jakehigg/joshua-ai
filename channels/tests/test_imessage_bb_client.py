"""BlueBubbles REST client. Offline via an httpx MockTransport — no Mac, no net."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from joshua_channels.adapters.imessage.bb_client import (
    ALREADY_QUEUED,
    BlueBubblesClient,
    BlueBubblesError,
    chunk_text,
)

BB_URL = "http://bb.test:1234"


def _client(handler: Callable[[httpx.Request], httpx.Response], **kwargs: Any) -> BlueBubblesClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return BlueBubblesClient(
        http, BB_URL, "bb-pass", min_interval_s=0.0, backoff_s=(0.0, 0.0, 0.0), **kwargs
    )


# ── chunk_text ───────────────────────────────────────────────────────────────


def test_chunk_text_hard_splits_in_order():
    assert chunk_text("", 4000) == []
    assert chunk_text("abcdef", 2) == ["ab", "cd", "ef"]
    assert len(chunk_text("x" * 9000, 4000)) == 3


def test_chunk_text_rejects_nonpositive_size():
    with pytest.raises(ValueError):
        chunk_text("hi", 0)


# ── configured guard ─────────────────────────────────────────────────────────


async def test_unconfigured_client_refuses_send():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    bb = BlueBubblesClient(http, BB_URL, "")  # no password
    assert bb.configured is False
    with pytest.raises(BlueBubblesError):
        await bb.send_text("chat1", "hi")


# ── send_text ────────────────────────────────────────────────────────────────


async def test_send_text_uses_message_field_and_apple_script():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"status": 200, "data": {}})

    bb = _client(handler)
    result = await bb.send_text("chat1", "hello")
    assert result.attempts == 1
    assert seen["message"] == "hello"  # NOT "text"
    assert seen["method"] == "apple-script"
    assert "tempGuid" in seen


async def test_send_text_treats_already_queued_400_as_success():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"send failed: {ALREADY_QUEUED}")

    bb = _client(handler)
    result = await bb.send_text("chat1", "hello")
    assert result.already_queued is True


async def test_send_text_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    bb = _client(handler, max_attempts=3)
    with pytest.raises(BlueBubblesError):
        await bb.send_text("chat1", "hello")
    assert calls["n"] == 3  # every attempt reuses the one tempGuid


async def test_send_chunks_reuses_distinct_temp_guids_per_chunk():
    guids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        guids.append(json.loads(request.content)["tempGuid"])
        return httpx.Response(200, json={"data": {}})

    bb = _client(handler)
    results = await bb.send_chunks("chat1", ["a", "b"])
    assert len(results) == 2
    assert len(set(guids)) == 2  # one tempGuid per chunk


# ── send_attachment ──────────────────────────────────────────────────────────


async def test_send_attachment_posts_multipart(tmp_path: Path):
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type", "")
        seen["body"] = request.content
        return httpx.Response(200, json={"data": {}})

    photo = tmp_path / "pic.jpg"
    photo.write_bytes(b"jpeg-bytes")
    bb = _client(handler)
    result = await bb.send_attachment("chat1", photo)
    assert result.attempts == 1
    assert "multipart/form-data" in seen["content_type"]
    assert b"jpeg-bytes" in seen["body"]


# ── read paths ───────────────────────────────────────────────────────────────


async def test_download_attachment_returns_bytes():
    bb = _client(lambda r: httpx.Response(200, content=b"raw-bytes"))
    assert await bb.download_attachment("g1") == b"raw-bytes"


async def test_download_attachment_raises_on_error():
    bb = _client(lambda r: httpx.Response(404, text="nope"))
    with pytest.raises(BlueBubblesError):
        await bb.download_attachment("g1")


async def test_query_messages_since_returns_rows():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": 200, "data": [{"guid": "A"}, "junk", {"guid": "B"}]}
        )

    bb = _client(handler)
    rows = await bb.query_messages_since(0, limit=10)
    assert [r["guid"] for r in rows] == ["A", "B"]  # non-dict rows dropped


async def test_ping_never_raises():
    ok = _client(lambda r: httpx.Response(200))
    assert await ok.ping() is True
    bad = _client(lambda r: httpx.Response(500))
    assert await bad.ping() is False
