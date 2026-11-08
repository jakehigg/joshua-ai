"""Core's model forwarder, ``/v1/model/{path}``: who may call it, which paths go
on, which credential the server gets, and what never leaves core.

An ``httpx`` mock transport stands in for the model server, so no test reaches
a network.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from engine_fakes import BASE_CONFIG
from fastapi import FastAPI
from joshua_core.model_forwarder import (
    ModelForwarder,
    path_allowed,
)
from joshua_shared import config as config_module
from joshua_shared.contracts import MODEL_FORWARDER_PATH
from joshua_shared.inference import resolve_inference
from starlette.testclient import TestClient

ASSIGNMENTS = "assignments-fleet-token"
CORE = "core-fleet-token"
VIEWER = "viewer-fleet-token"
OAUTH = "fake-subscription-token-0001"
API_KEY = "fake-api-key-0002"
SERVER_KEY = "fake-server-key-0003"
SECRET_ENV = {"INFERENCE_API_KEY": API_KEY, "SERVER_KEY": SERVER_KEY}
MESSAGES = f"{MODEL_FORWARDER_PATH}/v1/messages"
BODY = b'{"model":"claude-sonnet-5","max_tokens":10,"messages":[]}'


def streamed(status: int, body: bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    """A response whose body is a stream, as a real server gives."""

    async def chunks() -> AsyncIterator[bytes]:
        yield body

    return httpx.Response(status, headers=headers, content=chunks())


class Upstream:
    """A fake model server. It records each request and answers with
    ``response`` or raises ``error``."""

    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None):
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.response = response
        self.error = error

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(await request.aread())
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.response is not None:
            return self.response
        return streamed(200, b'{"type": "message"}', {"content-type": "application/json"})


def _settings(section: str = "") -> Any:
    return config_module.parse(BASE_CONFIG + section, env=SECRET_ENV, source="<test>")


def _client(
    monkeypatch: pytest.MonkeyPatch,
    upstream: Upstream,
    *,
    section: str = "",
    oauth: str = OAUTH,
) -> TestClient:
    monkeypatch.setenv("JOSHUA_TOKEN_ASSIGNMENTS", ASSIGNMENTS)
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE)
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    forwarder = ModelForwarder(
        client=http,
        resolver=lambda settings, job: resolve_inference(settings, job, oauth_token=oauth),
    )
    app = FastAPI()
    app.state.ctx = SimpleNamespace(settings=_settings(section))
    app.include_router(forwarder.router)
    return TestClient(app)


def _caller(token: str | None) -> dict[str, str]:
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ── the path allowlist ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    ["v1/messages", "v1/messages/count_tokens", "v1/models", "v1/models/claude-sonnet-5"],
)
def test_the_paths_of_the_cli_go_on(path: str) -> None:
    assert path_allowed(path)


@pytest.mark.parametrize(
    "path",
    [
        "v1/complete",
        "v1/messages/batches",
        "v1/models/a/b",
        "v1/models/..",
        "v1/models/a..b",
        "v1/files",
        "api/hello",
        "api/oauth/usage",
        "",
    ],
)
def test_any_other_path_is_refused(path: str) -> None:
    assert not path_allowed(path)


# ── who may call it ──────────────────────────────────────────────────────────


def test_the_assignments_token_reaches_the_server_with_the_job_credential(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, upstream)
    with caplog.at_level(logging.DEBUG):
        response = client.post(
            MESSAGES + "?beta=true",
            content=BODY,
            headers={**_caller(ASSIGNMENTS), "anthropic-beta": "oauth-2025-04-20"},
        )

    assert response.status_code == 200
    assert response.json() == {"type": "message"}
    [sent] = upstream.requests
    assert str(sent.url) == "https://api.anthropic.com/v1/messages?beta=true"
    assert sent.headers["authorization"] == f"Bearer {OAUTH}"
    assert sent.headers["host"] == "api.anthropic.com"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    # The subscription needs the OAuth flag, so it stays.
    assert sent.headers["anthropic-beta"] == "oauth-2025-04-20"
    assert "x-api-key" not in sent.headers
    assert ASSIGNMENTS not in repr(list(sent.headers.items()))
    assert upstream.bodies == [BODY]
    logged = "\n".join(f"{r.msg!r}" for r in caplog.records)
    for secret in (ASSIGNMENTS, OAUTH, "claude-sonnet-5"):
        assert secret not in logged


@pytest.mark.parametrize("token", [None, "not-a-token", CORE, VIEWER])
def test_no_token_a_wrong_token_and_another_identity_are_401(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, upstream)
    response = client.post(MESSAGES, content=BODY, headers=_caller(token))
    assert response.status_code == 401
    assert upstream.requests == []


def test_an_x_api_key_is_not_a_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, upstream)
    response = client.post(MESSAGES, content=BODY, headers={"x-api-key": ASSIGNMENTS})
    assert response.status_code == 401
    assert upstream.requests == []


def test_a_path_off_the_list_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, upstream)
    response = client.post(
        f"{MODEL_FORWARDER_PATH}/v1/complete", content=BODY, headers=_caller(ASSIGNMENTS)
    )
    assert response.status_code == 403
    assert upstream.requests == []


def test_a_path_off_the_list_with_no_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, Upstream())
    response = client.post(f"{MODEL_FORWARDER_PATH}/v1/complete", content=BODY)
    assert response.status_code == 401


def test_core_answers_the_cli_probe_with_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, upstream)
    assert client.get(f"{MODEL_FORWARDER_PATH}/api/hello").json() == {}
    assert client.head(f"{MODEL_FORWARDER_PATH}/api/hello").status_code == 200
    assert client.post(f"{MODEL_FORWARDER_PATH}/api/hello").status_code == 401
    assert upstream.requests == []


def test_an_empty_caller_list_turns_the_route_off(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = Upstream()
    section = "inference:\n  forwarder:\n    allowed_callers:\n"
    client = _client(monkeypatch, upstream, section=section)
    assert client.post(MESSAGES, content=BODY, headers=_caller(ASSIGNMENTS)).status_code == 404
    assert client.get(f"{MODEL_FORWARDER_PATH}/api/hello").status_code == 404
    assert upstream.requests == []


def test_the_caller_list_names_who_may_call(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = Upstream()
    section = "inference:\n  forwarder:\n    allowed_callers:\n      - Viewer\n"
    client = _client(monkeypatch, upstream, section=section)
    assert client.post(MESSAGES, content=BODY, headers=_caller(ASSIGNMENTS)).status_code == 401
    assert client.post(MESSAGES, content=BODY, headers=_caller(VIEWER)).status_code == 200


# ── the credential of the job ────────────────────────────────────────────────


def test_an_api_key_goes_as_x_api_key_and_drops_the_oauth_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = Upstream()
    section = "inference:\n  api_key: ${INFERENCE_API_KEY}\n"
    client = _client(monkeypatch, upstream, section=section)
    response = client.post(
        MESSAGES,
        content=BODY,
        headers={**_caller(ASSIGNMENTS), "anthropic-beta": "oauth-2025-04-20,tools-2024"},
    )
    assert response.status_code == 200
    [sent] = upstream.requests
    assert sent.headers["x-api-key"] == API_KEY
    assert "authorization" not in sent.headers
    assert sent.headers["anthropic-beta"] == "tools-2024"


def test_the_assignment_block_routes_to_another_server(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = Upstream()
    section = (
        "inference:\n  assignment:\n    base_url: http://llm.example.net:4000/v1\n"
        "    api_key: ${SERVER_KEY}\n"
    )
    client = _client(monkeypatch, upstream, section=section)
    response = client.post(
        MESSAGES,
        content=BODY,
        headers={**_caller(ASSIGNMENTS), "anthropic-beta": "oauth-2025-04-20"},
    )
    assert response.status_code == 200
    [sent] = upstream.requests
    assert str(sent.url) == "http://llm.example.net:4000/v1/messages"
    assert sent.headers["authorization"] == f"Bearer {SERVER_KEY}"
    assert sent.headers["host"] == "llm.example.net:4000"
    assert "anthropic-beta" not in sent.headers


def test_a_server_with_no_key_gets_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = Upstream()
    section = 'inference:\n  assignment:\n    base_url: http://llm.example.net\n    api_key: ""\n'
    client = _client(monkeypatch, upstream, section=section)
    response = client.post(MESSAGES, content=BODY, headers=_caller(ASSIGNMENTS))
    assert response.status_code == 200
    [sent] = upstream.requests
    assert "authorization" not in sent.headers
    assert "x-api-key" not in sent.headers


def test_no_subscription_token_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, upstream, oauth="")
    response = client.post(MESSAGES, content=BODY, headers=_caller(ASSIGNMENTS))
    assert response.status_code == 503
    assert upstream.requests == []


# ── the answer ───────────────────────────────────────────────────────────────


async def _events() -> AsyncIterator[bytes]:
    for n in range(5):
        yield f'event: content_block_delta\ndata: {{"n": {n}}}\n\n'.encode()


def test_a_streamed_answer_arrives_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = Upstream(
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_events())
    )
    client = _client(monkeypatch, upstream)
    with client.stream("POST", MESSAGES, content=BODY, headers=_caller(ASSIGNMENTS)) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        text = b"".join(response.iter_bytes()).decode()
    numbers = [line for line in text.splitlines() if line.startswith("data:")]
    assert numbers == [f'data: {{"n": {n}}}' for n in range(5)]


def test_a_streamed_request_body_goes_on_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, upstream)

    def parts() -> Any:
        yield b'{"model":'
        yield b'"claude-sonnet-5"}'

    response = client.post(MESSAGES, content=parts(), headers=_caller(ASSIGNMENTS))
    assert response.status_code == 200
    assert upstream.bodies == [b'{"model":"claude-sonnet-5"}']


def test_the_server_status_and_headers_go_back(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = Upstream(streamed(429, b'{"type": "error"}', {"retry-after": "30"}))
    client = _client(monkeypatch, upstream)
    response = client.post(MESSAGES, content=BODY, headers=_caller(ASSIGNMENTS))
    assert response.status_code == 429
    assert response.headers["retry-after"] == "30"


def test_a_server_that_does_not_answer_is_502_with_no_error_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    error = httpx.ConnectError("cannot connect to secret-host.example.net:443")
    client = _client(monkeypatch, Upstream(error=error))
    with caplog.at_level(logging.DEBUG):
        response = client.post(MESSAGES, content=BODY, headers=_caller(ASSIGNMENTS))
    assert response.status_code == 502
    assert response.json() == {"error": "model server unreachable"}
    logged = "\n".join(f"{r.msg!r}" for r in caplog.records)
    assert "secret-host" not in logged
    assert "ConnectError" in logged


# ── the client ───────────────────────────────────────────────────────────────


async def test_aclose_closes_only_the_client_it_made() -> None:
    made = ModelForwarder()
    client = made.http()
    await made.aclose()
    assert client.is_closed

    given = httpx.AsyncClient()
    forwarder = ModelForwarder(client=given)
    assert forwarder.http() is given
    await forwarder.aclose()
    assert not given.is_closed
    await given.aclose()


def test_core_serves_the_forwarder() -> None:
    from joshua_core.main import build_app

    app = build_app()
    assert isinstance(app.state.forwarder, ModelForwarder)
    app.state.ctx = SimpleNamespace(settings=_settings())
    client = TestClient(app)  # no lifespan: no database
    assert client.get(f"{MODEL_FORWARDER_PATH}/api/hello").json() == {}
    assert client.post(MESSAGES, content=BODY).status_code == 401
