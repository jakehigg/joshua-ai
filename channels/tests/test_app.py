"""App wiring tests: the no-channels warning and the served route set."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from joshua_channels import app as app_module
from joshua_channels.app import (
    build_app,
    build_context,
    configured_channel_types,
    imessage_enabled,
    telegram_enabled,
)
from joshua_channels.deliver import ChannelsContext
from joshua_channels.registry import AdapterRegistry
from joshua_shared import config as config_module

NO_CHANNELS = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
"""

BOTH_CHANNELS = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
channels:
  telegram:
    bot_token: t123
  imessage:
    bluebubbles_url: http://bb.local
    bluebubbles_password: pw
    webhook_path_secret: s
"""


EMPTY_CREDENTIALS = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
channels:
  telegram:
    bot_token: ""
  imessage:
    bluebubbles_url: http://bb.local
    bluebubbles_password: ""
    webhook_path_secret: ""
"""


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://channels")


@pytest.fixture(autouse=True)
def _core_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", "core-tok")
    monkeypatch.delenv("CORE_URL", raising=False)


def test_configured_channel_types() -> None:
    settings = config_module.parse(BOTH_CHANNELS, env={}, source="<test>")
    assert sorted(configured_channel_types(settings)) == ["imessage", "telegram"]


def test_empty_credentials_disable_a_channel() -> None:
    settings = config_module.parse(EMPTY_CREDENTIALS, env={}, source="<test>")
    assert telegram_enabled(settings) is False
    assert imessage_enabled(settings) is False
    assert configured_channel_types(settings) == []


def test_build_context_skips_a_channel_with_an_empty_credential(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An empty bot token must not register the adapter, so the app still boots."""
    settings = config_module.parse(EMPTY_CREDENTIALS, env={}, source="<test>")
    monkeypatch.setattr(config_module, "_cache", settings)
    with caplog.at_level("WARNING"):
        context, _ = build_context()
    # The terminal channel is always on; neither platform registers.
    assert context.registry.types() == ["cli"]
    messages = [r.msg["message"] for r in caplog.records if isinstance(r.msg, dict)]
    assert any("telegram configured with an empty bot token" in m for m in messages)
    assert any("imessage configured with an empty credential" in m for m in messages)


def test_build_context_warns_without_channels(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = config_module.parse(NO_CHANNELS, env={}, source="<test>")
    monkeypatch.setattr(config_module, "_cache", settings)
    with caplog.at_level("WARNING"):
        context, core_client = build_context()
    assert context.registry.types() == ["cli"]
    assert core_client is None  # CORE_URL unset
    assert any(
        isinstance(r.msg, dict) and "no platform channel" in r.msg["message"]
        for r in caplog.records
    )


async def test_serves_only_health_and_deliver() -> None:
    settings = config_module.parse(NO_CHANNELS, env={}, source="<test>")
    context = ChannelsContext(settings=settings, registry=AdapterRegistry())
    app = build_app(context)
    async with _client(app) as client:
        assert (await client.get("/healthz")).json() == {"ok": True}
        assert (await client.get("/readyz")).json()["ok"] is True
        # deliver is served but resolves nothing without an adapter.
        deliver = await client.post(
            "/v1/deliver",
            json={"channel": "telegram:1", "text": "hi"},
            headers={"Authorization": "Bearer core-tok"},
        )
        assert deliver.status_code == 404
        # No other route exists.
        assert (await client.get("/v1/other")).status_code == 404


def test_module_app_is_built() -> None:
    assert app_module.app.title == "joshua-channels"


async def test_lifespan_starts_and_stops_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    from adapter_fakes import FakeAdapter

    settings = config_module.parse(NO_CHANNELS, env={}, source="<test>")
    registry = AdapterRegistry()
    adapter = FakeAdapter("telegram")
    registry.register(adapter)
    context = ChannelsContext(settings=settings, registry=registry)

    class _Core:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    core = _Core()
    monkeypatch.setattr(app_module, "build_context", lambda: (context, core))

    app = build_app()
    async with app.router.lifespan_context(app):
        assert app.state.ctx is context
        assert adapter.started is True
    assert adapter.stopped is True
    assert core.closed is True
