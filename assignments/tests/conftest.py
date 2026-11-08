"""Fixtures for the assignments tests.

Every test runs offline: an in-memory store, a parsed config, and the ASGI
app in-process. The Postgres tests are marked ``integration``.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable
from typing import Any

import httpx
import httpx2
import pytest
from fakes import FIXED_NOW, FakeStore
from joshua_assignments.settings import Caller, Settings
from joshua_shared import config
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.types import ASGIApp

ALEX = "alex-token"
MIA = "mia-token"
SAM = "sam-token"
ADDON = "addon-token"
CHANNELS_TOKEN = "assignments-fleet-token"

CONFIG_TEXT = """
name: Home
timezone: UTC
people:
  - id: alex
    name: Alex
  - id: mia
    name: Mia
  - id: sam
    name: Sam
    role: guest
assignments:
  enabled: true
  repos:
    - gitlab.example.net/infra/*
  platforms:
    gitlab.example.net:
      kind: gitlab
      token_env: ASSIGNMENTS_GITLAB_TOKEN
  people:
    alex:
      notify: telegram:dm:alex
"""


def make_config(extra: str = "", text: str = CONFIG_TEXT) -> config.JoshuaConfig:
    return config.parse(text + extra, {}, source="test.yaml")


def make_settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "database_url": "postgresql://unused",
        "tokens": {
            ALEX: Caller(person="alex"),
            MIA: Caller(person="mia"),
            SAM: Caller(person="sam"),
            ADDON: Caller(person=None),
        },
        "channels_url": "http://channels:8000",
        "channels_token": CHANNELS_TOKEN,
    }
    fields.update(overrides)
    return Settings(**fields)


class FakeChannels:
    """A fake channels ``POST /v1/events``. ``replies`` gives the status of each call."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.replies: list[int | Exception] = []

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if self.replies else 202
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(reply, json={"accepted": reply == 202, "reason": "test"})


@pytest.fixture
def channels(monkeypatch) -> FakeChannels:
    """A fake channels behind every client that notify makes, with no wait between tries."""
    from joshua_assignments import notify

    fake = FakeChannels()
    monkeypatch.setattr(
        notify,
        "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    monkeypatch.setattr(notify, "RETRY_BACKOFF_S", 0.0)
    return fake


@pytest.fixture
def cfg() -> config.JoshuaConfig:
    return make_config()


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


def build(
    settings: Settings,
    store: FakeStore,
    cfg: config.JoshuaConfig,
    stub_delay_s: float | None = 0.0,
) -> ASGIApp:
    from joshua_assignments.server import build_app

    return build_app(
        settings, store, stub_delay_s=stub_delay_s, load_config=lambda: cfg, clock=lambda: FIXED_NOW
    )


@pytest.fixture
async def app(settings, store, cfg, channels) -> ASGIApp:
    """The app with a stub runner that ends each assignment at once."""
    from joshua_assignments.server import get_manager

    built = build(settings, store, cfg)
    await get_manager().open()
    return built


@pytest.fixture
async def holding_app(settings, store, cfg, channels) -> ASGIApp:
    """The app with a stub runner that never ends: an assignment stays running."""
    from joshua_assignments.server import get_manager

    built = build(settings, store, cfg, stub_delay_s=None)
    await get_manager().open()
    return built


@pytest.fixture(autouse=True)
async def forget_the_manager():
    """Cancel the stub runs of the app a test built, and forget its manager."""
    yield
    from joshua_assignments import server

    if server._manager is not None:
        for task in list(server._manager.runner.tasks.values()):
            task.cancel()
        server._manager = None


async def settle() -> None:
    """Wait until the stub runs that can end have ended."""
    from joshua_assignments.server import get_manager

    await get_manager().runner.idle()


def payload(result) -> dict:
    assert result.is_error is not True, result.content
    [block] = result.content
    return json.loads(block.text)


def error(result) -> str:
    assert result.is_error is True
    return result.content[0].text


@contextlib.asynccontextmanager
async def _client_session(app: ASGIApp, token: str | None, headers: dict[str, str]):
    sent = {"Authorization": f"Bearer {token}"} if token else {}
    sent.update(headers)
    transport = httpx2.ASGITransport(app=app)
    client = httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", headers=sent, timeout=10
    )
    try:
        async with streamable_http_client("http://testserver/mcp", http_client=client) as (
            read,
            write_stream,
        ):
            async with ClientSession(read, write_stream) as session:
                await session.initialize()
                yield session
    finally:
        await client.aclose()


@contextlib.asynccontextmanager
async def mcp_sessions(app: ASGIApp, *tokens: str | None, headers: dict[str, str] | None = None):
    """One MCP session for each token, over an in-process ASGI transport.

    An ASGI transport sends no ``lifespan`` event, so the session manager
    runs here.
    """
    from joshua_assignments.server import mcp

    async with mcp.session_manager.run():
        async with contextlib.AsyncExitStack() as stack:
            yield [
                await stack.enter_async_context(_client_session(app, t, headers or {}))
                for t in tokens
            ]


@contextlib.asynccontextmanager
async def mcp_session(app: ASGIApp, token: str | None = ALEX, **kwargs: Any):
    async with mcp_sessions(app, token, **kwargs) as (session,):
        yield session


Spec = Callable[..., dict[str, Any]]


def spec(**fields: Any) -> dict[str, Any]:
    return {"goal": "Audit the gitops repo\nand file one issue per finding.", **fields}
