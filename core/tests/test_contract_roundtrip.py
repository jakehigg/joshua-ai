"""Contract tests for the core → channels wire: deliver and resolve.

Core runs a turn and delivers the reply to a fake channels app; a separate test
resolves a destination ref through the same fake. Both sides use the shared
contract models, so a drift in ``DeliverRequest`` or ``ResolveResponse`` fails
here. A stub manager keeps the path offline — no DB, no SDK, no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fakes.channels import FakeChannels
from joshua_core.delivery import Deliverer
from joshua_core.engine.types import TurnResult
from joshua_core.events import ChannelsResolver
from joshua_core.main import build_app
from joshua_core.store.models import Channel, Conversation, Person
from joshua_core.turns import TurnService
from joshua_shared import config as config_module
from joshua_shared.http import FleetClient

CHANNELS_TOKEN = "chan-tok"
CORE_TOKEN = "core-tok"

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    handles:
      telegram: "998877"
"""


class StubManager:
    """A manager whose turn echoes a stub reply, no engine or SDK."""

    async def run_turn(
        self, channel: Any, conversation: Any, text: str, **kwargs: Any
    ) -> TurnResult:
        return TurnResult(text=f"(stub) {text}")


class StubRepo:
    """The minimum store the turn resolution needs, all in memory."""

    def __init__(self) -> None:
        self.people = {"alex": Person(id="alex", display_name="Alex", role="member")}
        self.handles = {("telegram", "998877"): "alex"}
        self.channels: dict[str, Channel] = {}
        self.conversations: dict[tuple[str, str | None], Conversation] = {}
        self._next = 0

    async def get_person(self, person_id: str) -> Person | None:
        return self.people.get(person_id)

    async def get_person_by_handle(self, channel_type: str, handle: str) -> Person | None:
        person_id = self.handles.get((channel_type, handle))
        return self.people.get(person_id) if person_id else None

    async def get_channel(self, channel_id: str) -> Channel | None:
        return self.channels.get(channel_id)

    async def upsert_channel(
        self,
        channel_id: str,
        channel_type: str,
        display_name: str | None = None,
        default_person_id: str | None = None,
        config: dict[str, Any] | None = None,
        session_mode: str = "per_person",
    ) -> None:
        self.channels[channel_id] = Channel(
            id=channel_id,
            channel_type=channel_type,
            display_name=display_name,
            default_person_id=default_person_id,
            session_mode=session_mode,
        )

    async def get_or_create_conversation(
        self, channel_id: str, person_id: str | None
    ) -> Conversation:
        key = (channel_id, person_id)
        conv = self.conversations.get(key)
        if conv is None:
            self._next += 1
            conv = Conversation(id=f"conv-{self._next}", channel_id=channel_id, person_id=person_id)
            self.conversations[key] = conv
        return conv


def _settings() -> Any:
    return config_module.parse(CONFIG, env={}, source="<test>")


@pytest.fixture(autouse=True)
def _fleet_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", CHANNELS_TOKEN)
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE_TOKEN)


def _core_client(turns: TurnService) -> httpx.AsyncClient:
    app = build_app()
    app.state.ctx = SimpleNamespace(turns=turns)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://core")


async def test_turn_delivers_deliver_request_to_channels() -> None:
    fake = FakeChannels()
    client = FleetClient(
        "http://channels", CORE_TOKEN, transport=httpx.ASGITransport(app=fake.build_app())
    )
    turns = TurnService(
        repo=StubRepo(),
        settings=_settings(),
        manager=StubManager(),
        deliverer=Deliverer(client),
    )

    event = {
        "channel": "telegram:998877",
        "chat": {"kind": "dm", "id": "998877"},
        "handle": {"type": "telegram", "id": "998877"},
        "text": "hello there",
        "message_id": "m-1",
    }
    async with _core_client(turns) as core:
        resp = await core.post(
            "/v1/turns", json=event, headers={"Authorization": f"Bearer {CHANNELS_TOKEN}"}
        )
        assert resp.status_code == 202
        await turns.drain()

    assert len(fake.delivered) == 1
    delivered = fake.delivered[0]
    assert delivered.channel == "telegram:998877"
    assert delivered.text == "(stub) hello there"
    assert delivered.attachments == []

    await client.aclose()


async def test_resolver_reads_resolve_response() -> None:
    fake = FakeChannels(resolvable={"everyone": ("telegram:-100", "group", "Everyone")})
    client = FleetClient(
        "http://channels", CORE_TOKEN, transport=httpx.ASGITransport(app=fake.build_app())
    )
    resolver = ChannelsResolver(client)

    resolved = await resolver.resolve("everyone")
    assert resolved is not None
    assert resolved.channel_id == "telegram:-100"
    assert resolved.chat_kind == "group"

    missing = await resolver.resolve("nope")
    assert missing is None

    await client.aclose()
