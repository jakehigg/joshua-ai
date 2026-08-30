"""Turns-API tests: routing, idempotency, delivery, streaming, and auth.

The core app runs over an httpx ``ASGITransport``; the deliverer points at a fake
channels app over another ``ASGITransport``. A fake repo and the stub agent keep
the whole path offline — no DB, no SDK, no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from engine_fakes import FakeComposer, fake_derive_profile
from fastapi import FastAPI, Request
from joshua_core.delivery import Deliverer
from joshua_core.engine.manager import ConversationManager
from joshua_core.engine.types import TurnResult
from joshua_core.main import build_app
from joshua_core.store.models import Channel, Conversation, Person
from joshua_core.turns import TURN_FAILED, TurnService
from joshua_shared import config as config_module
from joshua_shared.http import FleetClient

CHANNELS_TOKEN = "chan-tok"
CORE_TOKEN = "core-tok"
LAPTOP_TOKEN = "laptop-tok"

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    handles:
      telegram: "998877"
groups:
  - id: fam
    channel: telegram
    chat_id: "-100"
core:
  pool_max: 20
"""


def make_settings(**overrides: Any) -> Any:
    text = CONFIG
    for key, value in overrides.items():
        text += f"\n# {key}={value}"
    return config_module.parse(text, env={}, source="<test>")


class TurnsFakeRepo:
    """In-memory store for resolution and the manager's transcript writes."""

    def __init__(self) -> None:
        self.people = {"alex": Person(id="alex", display_name="Alex", role="member")}
        self.handles = {("telegram", "998877"): "alex"}
        self.channels: dict[str, Channel] = {}
        self.conversations: dict[tuple[str, str | None], Conversation] = {}
        self.transcripts: list[dict[str, Any]] = []
        self.sdk_sessions: dict[str, str] = {}
        self.touched: list[str] = []
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

    async def add_transcript(
        self,
        conversation_id: str,
        direction: str,
        content: str,
        status: str = "ok",
        meta: dict[str, Any] | None = None,
    ) -> None:
        self.transcripts.append(
            {
                "conversation_id": conversation_id,
                "direction": direction,
                "content": content,
                "status": status,
                "meta": meta or {},
            }
        )

    async def set_sdk_session(
        self, conversation_id: str, session_id: str, last_channel_id: str | None = None
    ) -> None:
        self.sdk_sessions[conversation_id] = session_id

    async def touch_conversation(
        self, conversation_id: str, last_channel_id: str | None = None
    ) -> None:
        self.touched.append(conversation_id)


class FailingManager:
    """A manager whose turn always raises, to drive the failure-delivery path."""

    async def run_turn(self, *args: Any, **kwargs: Any) -> TurnResult:
        raise RuntimeError("boom")


def make_fake_channels(received: list[dict[str, Any]]) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/deliver")
    async def deliver(request: Request) -> dict[str, bool]:
        received.append(await request.json())
        return {"ok": True}

    return app


def build_turns(
    repo: TurnsFakeRepo,
    settings: Any,
    received: list[dict[str, Any]],
    tmp_path: Any,
    *,
    manager: Any | None = None,
) -> TurnService:
    if manager is None:
        manager = ConversationManager(
            repo=repo,
            settings=settings,
            composer=FakeComposer(),
            derive_profile=fake_derive_profile,
            agent_backend="stub",
            data_dir=tmp_path,
        )
    client = FleetClient(
        "http://channels",
        CORE_TOKEN,
        transport=httpx.ASGITransport(app=make_fake_channels(received)),
    )
    deliverer = Deliverer(client)
    return TurnService(repo=repo, settings=settings, manager=manager, deliverer=deliverer)


def core_client(turns: TurnService) -> httpx.AsyncClient:
    app = build_app()
    app.state.ctx = SimpleNamespace(turns=turns)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://core")


@pytest.fixture(autouse=True)
def _fleet_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", CHANNELS_TOKEN)
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE_TOKEN)
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP_TOKEN)


def dm_event(**overrides: Any) -> dict[str, Any]:
    event = {
        "channel": "telegram:998877",
        "chat": {"kind": "dm", "id": "998877"},
        "handle": {"type": "telegram", "id": "998877"},
        "text": "hello there",
        "message_id": "m-1",
    }
    event.update(overrides)
    return event


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_dm_turn_delivers_stub_reply(tmp_path) -> None:
    repo = TurnsFakeRepo()
    received: list[dict[str, Any]] = []
    turns = build_turns(repo, make_settings(), received, tmp_path)

    async with core_client(turns) as client:
        resp = await client.post("/v1/turns", json=dm_event(), headers=bearer(CHANNELS_TOKEN))
        assert resp.status_code == 202
        assert resp.json()["accepted"] is True
        await turns.drain()

    assert len(received) == 1
    assert received[0]["channel"] == "telegram:998877"
    assert received[0]["text"].startswith("(stub)")
    assert repo.channels["telegram:998877"].session_mode == "per_person"


async def test_group_turn_is_speaker_labelled(tmp_path) -> None:
    repo = TurnsFakeRepo()
    received: list[dict[str, Any]] = []
    turns = build_turns(repo, make_settings(), received, tmp_path)
    event = {
        "channel": "telegram:-100",
        "chat": {"kind": "group", "id": "-100", "title": "Everyone"},
        "handle": {"type": "telegram", "id": "998877"},
        "text": "hi all",
        "message_id": "g-1",
    }

    async with core_client(turns) as client:
        resp = await client.post("/v1/turns", json=event, headers=bearer(CHANNELS_TOKEN))
        assert resp.status_code == 202
        await turns.drain()

    in_row = next(r for r in repo.transcripts if r["direction"] == "in")
    assert in_row["meta"]["speaker"] == "Alex"
    assert repo.channels["telegram:-100"].session_mode == "shared"


async def test_unknown_handle_leaves_store_unchanged(tmp_path) -> None:
    repo = TurnsFakeRepo()
    received: list[dict[str, Any]] = []
    turns = build_turns(repo, make_settings(), received, tmp_path)
    event = dm_event(
        channel="telegram:404",
        chat={"kind": "dm", "id": "404"},
        handle={"type": "telegram", "id": "000"},
    )

    async with core_client(turns) as client:
        resp = await client.post("/v1/turns", json=event, headers=bearer(CHANNELS_TOKEN))

    assert resp.status_code == 403
    assert resp.json() == {"reason": "unknown_sender"}
    assert repo.channels == {}
    assert repo.transcripts == []
    assert received == []


async def test_duplicate_message_id_not_rerun(tmp_path) -> None:
    repo = TurnsFakeRepo()
    received: list[dict[str, Any]] = []
    turns = build_turns(repo, make_settings(), received, tmp_path)

    async with core_client(turns) as client:
        first = await client.post("/v1/turns", json=dm_event(), headers=bearer(CHANNELS_TOKEN))
        await turns.drain()
        second = await client.post("/v1/turns", json=dm_event(), headers=bearer(CHANNELS_TOKEN))
        await turns.drain()

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {"accepted": False, "reason": "duplicate"}
    assert len(received) == 1


async def test_turn_failure_delivers_fallback(tmp_path) -> None:
    repo = TurnsFakeRepo()
    received: list[dict[str, Any]] = []
    turns = build_turns(repo, make_settings(), received, tmp_path, manager=FailingManager())

    async with core_client(turns) as client:
        resp = await client.post("/v1/turns", json=dm_event(), headers=bearer(CHANNELS_TOKEN))
        assert resp.status_code == 202
        await turns.drain()

    assert received == [{"channel": "telegram:998877", "text": TURN_FAILED, "attachments": []}]


async def test_capacity_rejects_with_429(tmp_path) -> None:
    repo = TurnsFakeRepo()
    received: list[dict[str, Any]] = []
    settings = make_settings()
    settings.core.pool_max = 0
    turns = build_turns(repo, settings, received, tmp_path)

    async with core_client(turns) as client:
        resp = await client.post("/v1/turns", json=dm_event(), headers=bearer(CHANNELS_TOKEN))

    assert resp.status_code == 429
    assert resp.json()["reason"] == "overloaded"


async def test_stream_yields_delta_then_done(tmp_path) -> None:
    repo = TurnsFakeRepo()
    received: list[dict[str, Any]] = []
    turns = build_turns(repo, make_settings(), received, tmp_path)
    event = dm_event(
        channel="cli:alex",
        chat={"kind": "dm", "id": "alex"},
        handle={"type": "cli", "id": "alex"},
        message_id=None,
    )

    async with core_client(turns) as client:
        async with client.stream(
            "POST", "/v1/turns/stream", json=event, headers=bearer(CHANNELS_TOKEN)
        ) as resp:
            assert resp.status_code == 200
            body = ""
            async for chunk in resp.aiter_text():
                body += chunk

    assert "event: delta" in body
    assert "event: done" in body
    assert body.index("event: delta") < body.index("event: done")
    assert received == []


async def test_missing_bearer_is_401(tmp_path) -> None:
    repo = TurnsFakeRepo()
    turns = build_turns(repo, make_settings(), [], tmp_path)
    async with core_client(turns) as client:
        resp = await client.post("/v1/turns", json=dm_event())
    assert resp.status_code == 401


async def test_core_or_laptop_forbidden_on_turns(tmp_path) -> None:
    repo = TurnsFakeRepo()
    turns = build_turns(repo, make_settings(), [], tmp_path)
    async with core_client(turns) as client:
        for token in (CORE_TOKEN, LAPTOP_TOKEN):
            resp = await client.post("/v1/turns", json=dm_event(), headers=bearer(token))
            assert resp.status_code == 403


async def test_laptop_stream_needs_cli_handle(tmp_path) -> None:
    repo = TurnsFakeRepo()
    received: list[dict[str, Any]] = []
    turns = build_turns(repo, make_settings(), received, tmp_path)

    async with core_client(turns) as client:
        telegram = await client.post(
            "/v1/turns/stream", json=dm_event(message_id=None), headers=bearer(LAPTOP_TOKEN)
        )
        assert telegram.status_code == 403

        cli_event = dm_event(
            channel="cli:alex",
            chat={"kind": "dm", "id": "alex"},
            handle={"type": "cli", "id": "alex"},
            message_id=None,
        )
        async with client.stream(
            "POST", "/v1/turns/stream", json=cli_event, headers=bearer(LAPTOP_TOKEN)
        ) as resp:
            assert resp.status_code == 200
