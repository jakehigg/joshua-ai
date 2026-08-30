import httpx
import pytest
from joshua_shared import http


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "BACKOFF_BASE_S", 0.0)


async def test_sends_bearer_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"ok": True})

    client = http.FleetClient("http://svc", "tok-core", transport=httpx.MockTransport(handler))
    async with client:
        resp = await client.get_json("/thing")
    assert resp.status_code == 200
    assert seen["auth"] == "Bearer tok-core"


async def test_post_json_sends_body() -> None:
    seen: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(201)

    client = http.FleetClient("http://svc", "t", transport=httpx.MockTransport(handler))
    async with client:
        resp = await client.post_json("/thing", json={"a": 1})
    assert resp.status_code == 201
    assert b'"a"' in seen["body"]


async def test_retries_connection_errors_then_succeeds() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200)

    client = http.FleetClient("http://svc", "t", transport=httpx.MockTransport(handler))
    async with client:
        resp = await client.get_json("/thing")
    assert resp.status_code == 200
    assert attempts["n"] == 3


async def test_gives_up_after_max_attempts() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError("boom", request=request)

    client = http.FleetClient("http://svc", "t", transport=httpx.MockTransport(handler))
    async with client:
        with pytest.raises(httpx.ConnectError):
            await client.get_json("/thing")
    assert attempts["n"] == http.MAX_ATTEMPTS


async def test_stream_carries_bearer_and_yields_body() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, text="event: ping\ndata: 1\n\n")

    client = http.FleetClient("http://svc", "tok", transport=httpx.MockTransport(handler))
    async with client:
        async with client.stream("POST", "/sse", json={}) as response:
            lines = [line async for line in response.aiter_lines()]
    assert seen["auth"] == "Bearer tok"
    assert "event: ping" in lines


async def test_does_not_retry_4xx() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404)

    client = http.FleetClient("http://svc", "t", transport=httpx.MockTransport(handler))
    async with client:
        resp = await client.post_json("/thing", json={})
    assert resp.status_code == 404
    assert attempts["n"] == 1
