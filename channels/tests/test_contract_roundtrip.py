"""End-to-end contract test: a Telegram update round-trips through core and back.

The flow runs in one process over ASGI transports:

1. A fake Telegram update reaches the adapter.
2. The adapter posts a ``TurnEvent`` to a fake core (``/v1/turns``).
3. The fake core calls back ``/v1/deliver`` on the channels app under test.
4. The deliver route sends the reply to the fake Telegram bot.

The test asserts the reply text arrives at the bot, so the turn and deliver
contracts hold on both sides of the wire.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fakes.core import FakeCore
from joshua_channels.adapters.telegram import TelegramAdapter
from joshua_channels.app import build_app
from joshua_channels.core_client import CoreClient
from joshua_channels.deliver import ChannelsContext
from joshua_channels.guard import Guard
from joshua_channels.registry import AdapterRegistry
from joshua_shared import config as config_module
from joshua_shared.http import FleetClient

CHANNELS_TOKEN = "chan-tok"
CORE_TOKEN = "core-tok"

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex Diaz
    handles:
      telegram: "12345"
channels:
  telegram:
    bot_token: t-token
  limits:
    per_handle_per_minute: 20
"""


class FakeBot:
    """Records the outbound calls the adapter makes."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.messages.append(kwargs)

    async def send_chat_action(self, **kwargs: Any) -> None:
        self.actions.append(kwargs)


def _update(*, text: str = "ping", chat_id: str = "12345", user_id: int = 12345) -> SimpleNamespace:
    message = SimpleNamespace(text=text, caption=None, photo=None, document=None, message_id=1)
    user = SimpleNamespace(id=user_id, full_name="Alex Diaz")
    chat = SimpleNamespace(id=chat_id, type="private", title=None)
    return SimpleNamespace(effective_message=message, effective_user=user, effective_chat=chat)


@pytest.fixture(autouse=True)
def _fleet_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", CHANNELS_TOKEN)
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE_TOKEN)


async def test_telegram_turn_round_trips_to_bot(tmp_path) -> None:
    cfg = config_module.parse(CONFIG, env={}, source="<test>")
    bot = FakeBot()
    guard = Guard(cfg, cfg.channels.limits, config_provider=lambda: cfg)
    adapter = TelegramAdapter(
        bot_token="t-token",
        guard=guard,
        core_client=None,
        settings_provider=lambda: cfg,
        data_dir=str(tmp_path),
        bot=bot,
    )
    registry = AdapterRegistry()
    registry.register(adapter)
    context = ChannelsContext(settings=cfg, registry=registry, data_dir=str(tmp_path))
    channels_app = build_app(context)

    # The fake core delivers back to the channels app; the adapter posts to the
    # fake core. Build the deliver client first, then wire the adapter's core
    # client, so the two apps reference each other without a construction cycle.
    channels_client = FleetClient(
        "http://channels", CORE_TOKEN, transport=httpx.ASGITransport(app=channels_app)
    )
    fake_core = FakeCore(reply=lambda e: f"you said: {e.text}", deliver_client=channels_client)
    adapter._core = CoreClient(
        FleetClient("http://core", CHANNELS_TOKEN, transport=httpx.ASGITransport(app=fake_core.app))
    )

    await adapter._on_message(_update(text="ping"), SimpleNamespace(bot=bot))

    # The turn reached core with the normalized shape.
    assert len(fake_core.events) == 1
    event = fake_core.events[0]
    assert event.channel == "telegram:12345"
    assert event.text == "ping"
    assert event.handle is not None and event.handle.type == "telegram"

    # The reply round-tripped back to the bot.
    assert [m["text"] for m in bot.messages] == ["you said: ping"]

    await channels_client.aclose()
    await adapter._core.aclose()


async def test_unknown_channel_deliver_is_not_sent(tmp_path) -> None:
    # A reply to a channel the registry cannot resolve fails deliver (404) and
    # never reaches a bot.
    cfg = config_module.parse(CONFIG, env={}, source="<test>")
    bot = FakeBot()
    guard = Guard(cfg, cfg.channels.limits, config_provider=lambda: cfg)
    adapter = TelegramAdapter(
        bot_token="t-token",
        guard=guard,
        core_client=None,
        settings_provider=lambda: cfg,
        data_dir=str(tmp_path),
        bot=bot,
    )
    registry = AdapterRegistry()
    registry.register(adapter)
    channels_app = build_app(
        ChannelsContext(settings=cfg, registry=registry, data_dir=str(tmp_path))
    )
    channels_client = FleetClient(
        "http://channels", CORE_TOKEN, transport=httpx.ASGITransport(app=channels_app)
    )

    # Deliver to a channel type with no registered adapter fails resolution.
    resp = await channels_client.post_json(
        "/v1/deliver", json={"channel": "signal:99999", "text": "hi"}
    )
    assert resp.status_code == 404
    assert bot.messages == []

    await channels_client.aclose()
