"""Tests for the core → channels delivery client."""

from __future__ import annotations

import httpx
import pytest
from joshua_core import delivery
from joshua_core.delivery import Deliverer, build_deliverer, is_ignore
from joshua_shared import http
from joshua_shared.http import FleetClient


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "BACKOFF_BASE_S", 0.0)


def _deliverer(handler, *, token: str = "tok-core", dry_run: bool = False) -> Deliverer:
    client = FleetClient("http://channels", token, transport=httpx.MockTransport(handler))
    return Deliverer(client, dry_run=dry_run)


async def test_success_returns_true_with_body_and_bearer() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    deliverer = _deliverer(handler)
    async with deliverer._client:
        ok = await deliverer.deliver("telegram:123", "hello", ["attachments/2026/08/a.jpg"])

    assert ok is True
    assert seen["auth"] == "Bearer tok-core"
    assert seen["path"] == "/v1/deliver"
    assert b'"channel"' in seen["body"]
    assert b"telegram:123" in seen["body"]
    assert b"attachments/2026/08/a.jpg" in seen["body"]


async def test_unknown_channel_404_returns_false_no_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404, json={"error": "unknown_channel"})

    deliverer = _deliverer(handler)
    with caplog.at_level("WARNING"):
        async with deliverer._client:
            ok = await deliverer.deliver("telegram:nope", "hi")

    assert ok is False
    assert attempts["n"] == 1
    assert any(
        isinstance(r.msg, dict) and r.msg["message"] == "deliver failed: unknown channel"
        for r in caplog.records
    )


async def test_server_error_retries_three_times_then_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, text="boom")

    deliverer = _deliverer(handler)
    with caplog.at_level("WARNING"):
        async with deliverer._client:
            ok = await deliverer.deliver("telegram:123", "hi")

    assert ok is False
    assert attempts["n"] == http.MAX_ATTEMPTS
    warnings = [r for r in caplog.records if isinstance(r.msg, dict) and r.msg.get("status") == 500]
    assert len(warnings) == http.MAX_ATTEMPTS


async def test_recovers_when_a_retry_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200)

    deliverer = _deliverer(handler)
    async with deliverer._client:
        ok = await deliverer.deliver("telegram:123", "hi")

    assert ok is True
    assert attempts["n"] == 3


async def test_connection_error_returns_false() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    deliverer = _deliverer(handler)
    async with deliverer._client:
        ok = await deliverer.deliver("telegram:123", "hi")

    assert ok is False
    # FleetClient retries the connection itself, then raises; the deliverer stops.
    assert attempts["n"] == http.MAX_ATTEMPTS


async def test_other_4xx_returns_false_no_retry() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400)

    deliverer = _deliverer(handler)
    async with deliverer._client:
        ok = await deliverer.deliver("telegram:123", "hi")

    assert ok is False
    assert attempts["n"] == 1


async def test_dry_run_makes_no_call(caplog: pytest.LogCaptureFixture) -> None:
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200)

    deliverer = _deliverer(handler, dry_run=True)
    with caplog.at_level("INFO"):
        async with deliverer._client:
            ok = await deliverer.deliver("telegram:123", "hello")

    assert ok is True
    assert called["n"] == 0
    record = next(
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg["message"].startswith("deliver (dry-run)")
    )
    assert record["target"] == "telegram:123"
    assert record["chars"] == len("hello")


def test_is_ignore() -> None:
    assert is_ignore("[IGNORE]") is True
    assert is_ignore("  [IGNORE]  ") is True
    assert is_ignore("") is True
    assert is_ignore("   ") is True
    assert is_ignore("hello") is False
    assert is_ignore("[IGNORE] and more") is False


def test_dry_run_enabled_reads_env() -> None:
    assert delivery.dry_run_enabled({"DELIVER_DRY_RUN": "1"}) is True
    assert delivery.dry_run_enabled({"DELIVER_DRY_RUN": "0"}) is False
    assert delivery.dry_run_enabled({}) is False


async def test_build_deliverer_dry_run_from_env() -> None:
    deliverer = build_deliverer(
        {"CHANNELS_URL": "http://channels", "JOSHUA_TOKEN_CORE": "t", "DELIVER_DRY_RUN": "1"}
    )
    async with deliverer._client:
        ok = await deliverer.deliver("telegram:123", "hi")
    assert ok is True


def test_build_deliverer_requires_channels_url() -> None:
    with pytest.raises(RuntimeError, match="CHANNELS_URL"):
        build_deliverer({"JOSHUA_TOKEN_CORE": "t"})
