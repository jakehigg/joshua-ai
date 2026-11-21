"""Turns-API tests: routing, idempotency, delivery, streaming, and auth.

The core app runs over an httpx ``ASGITransport``; the deliverer points at a fake
channels app over another ``ASGITransport``. A fake repo and the stub agent keep
the whole path offline — no DB, no SDK, no network.
"""

from __future__ import annotations

import asyncio
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
from joshua_core.turns import (
    HOLD_DROPPED,
    HOLD_MAX,
    TURN_FAILED,
    DraftStore,
    HoldStore,
    TurnService,
)
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
        self,
        conversation_id: str,
        session_id: str,
        last_channel_id: str | None = None,
        inference_endpoint: str | None = None,
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


class BlockingManager:
    """A manager whose first turn waits, so a message arrives while it runs.

    ``started`` is set once the first turn runs, and ``gate`` releases it.
    ``texts`` holds the text of each turn, in order.
    """

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.gate = asyncio.Event()
        self.texts: list[str] = []

    async def run_turn(self, channel: Any, conversation: Any, text: str, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.texts.append(text)
        if len(self.texts) == 1:
            self.started.set()
            await self.gate.wait()
        return TurnResult(text=f"reply {len(self.texts)}")


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


# --- the fold ---------------------------------------------------------------


async def test_messages_that_arrive_during_a_turn_join_the_next_turn(tmp_path) -> None:
    repo = TurnsFakeRepo()
    received: list[dict[str, Any]] = []
    manager = BlockingManager()
    turns = build_turns(repo, make_settings(), received, tmp_path, manager=manager)

    async with core_client(turns) as client:
        first = await client.post("/v1/turns", json=dm_event(), headers=bearer(CHANNELS_TOKEN))
        await manager.started.wait()
        second = await client.post(
            "/v1/turns",
            json=dm_event(text="in the afternoon", message_id="m-2"),
            headers=bearer(CHANNELS_TOKEN),
        )
        third = await client.post(
            "/v1/turns",
            json=dm_event(text="no, in the morning", message_id="m-3"),
            headers=bearer(CHANNELS_TOKEN),
        )
        manager.gate.set()
        await turns.drain()

    assert first.json()["turn_id"]
    assert second.json() == {"accepted": True, "folded": True}
    assert third.json() == {"accepted": True, "folded": True}
    # The two messages reach the agent together, as one turn, one per line.
    assert manager.texts == ["hello there", "in the afternoon\nno, in the morning"]
    assert [r["text"] for r in received] == ["reply 1", "reply 2"]


async def test_a_folded_message_that_waited_too_long_is_dropped(tmp_path) -> None:
    repo = TurnsFakeRepo()
    received: list[dict[str, Any]] = []
    manager = BlockingManager()
    settings = make_settings()
    settings.core.hold_max_age_s = 0.01
    turns = build_turns(repo, settings, received, tmp_path, manager=manager)

    async with core_client(turns) as client:
        await client.post("/v1/turns", json=dm_event(), headers=bearer(CHANNELS_TOKEN))
        await manager.started.wait()
        await client.post(
            "/v1/turns",
            json=dm_event(text="and one more", message_id="m-2"),
            headers=bearer(CHANNELS_TOKEN),
        )
        await asyncio.sleep(0.05)
        manager.gate.set()
        await turns.drain()

    # The message never runs hours later: it is dropped, and the person is told.
    assert manager.texts == ["hello there"]
    assert [r["text"] for r in received] == ["reply 1", HOLD_DROPPED]


# --- the hold ---------------------------------------------------------------


def thread_body(**overrides: Any) -> dict[str, Any]:
    """The body of a hold or a draft call: the thread, the person, the text."""
    body = {"channel": "cli:alex", "person": "alex"}
    body.update(overrides)
    return body


async def test_a_message_sent_while_a_turn_runs_is_held(tmp_path) -> None:
    turns = build_turns(TurnsFakeRepo(), make_settings(), [], tmp_path)

    async with core_client(turns) as client:
        added = await client.post(
            "/v1/holds", json=thread_body(text="  and one more  "), headers=bearer(CHANNELS_TOKEN)
        )
        listed = await client.get(
            "/v1/holds",
            params={"channel": "cli:alex", "person": "alex"},
            headers=bearer(CHANNELS_TOKEN),
        )

    assert added.status_code == 200
    assert added.json()["message"]["text"] == "and one more"
    held = listed.json()["held"]
    assert [m["text"] for m in held] == ["and one more"]
    assert held[0]["id"] == added.json()["message"]["id"]
    assert held[0]["at"]


async def test_take_gives_the_held_messages_as_one_turn_and_empties_the_hold(tmp_path) -> None:
    turns = build_turns(TurnsFakeRepo(), make_settings(), [], tmp_path)

    async with core_client(turns) as client:
        for text in ("first", "second", "third"):
            await client.post(
                "/v1/holds", json=thread_body(text=text), headers=bearer(CHANNELS_TOKEN)
            )
        taken = await client.post(
            "/v1/holds/take", json=thread_body(), headers=bearer(CHANNELS_TOKEN)
        )
        again = await client.post(
            "/v1/holds/take", json=thread_body(), headers=bearer(CHANNELS_TOKEN)
        )

    assert taken.json()["text"] == "first\nsecond\nthird"
    assert [m["text"] for m in taken.json()["held"]] == ["first", "second", "third"]
    # The second caller finds nothing: the messages go as one turn, once.
    assert again.json() == {"text": "", "held": [], "dropped": []}


async def test_a_held_message_can_be_removed_before_it_goes(tmp_path) -> None:
    turns = build_turns(TurnsFakeRepo(), make_settings(), [], tmp_path)

    async with core_client(turns) as client:
        first = await client.post(
            "/v1/holds", json=thread_body(text="keep"), headers=bearer(CHANNELS_TOKEN)
        )
        second = await client.post(
            "/v1/holds", json=thread_body(text="drop"), headers=bearer(CHANNELS_TOKEN)
        )
        hold_id = second.json()["message"]["id"]
        removed = await client.delete(
            f"/v1/holds/{hold_id}",
            params={"channel": "cli:alex", "person": "alex"},
            headers=bearer(CHANNELS_TOKEN),
        )
        missing = await client.delete(
            f"/v1/holds/{hold_id}",
            params={"channel": "cli:alex", "person": "alex"},
            headers=bearer(CHANNELS_TOKEN),
        )

    assert removed.status_code == 200
    assert [m["id"] for m in removed.json()["held"]] == [first.json()["message"]["id"]]
    assert missing.status_code == 404
    assert missing.json() == {"reason": "unknown_hold"}


async def test_each_thread_holds_its_own_messages(tmp_path) -> None:
    turns = build_turns(TurnsFakeRepo(), make_settings(), [], tmp_path)

    async with core_client(turns) as client:
        await client.post(
            "/v1/holds", json=thread_body(text="main"), headers=bearer(CHANNELS_TOKEN)
        )
        await client.post(
            "/v1/holds",
            json=thread_body(channel="workspace:solar", text="in the workspace"),
            headers=bearer(CHANNELS_TOKEN),
        )
        main = await client.get(
            "/v1/holds",
            params={"channel": "cli:alex", "person": "alex"},
            headers=bearer(CHANNELS_TOKEN),
        )
        workspace = await client.get(
            "/v1/holds",
            params={"channel": "workspace:solar", "person": "alex"},
            headers=bearer(CHANNELS_TOKEN),
        )

    assert [m["text"] for m in main.json()["held"]] == ["main"]
    assert [m["text"] for m in workspace.json()["held"]] == ["in the workspace"]


async def test_an_empty_message_is_not_held(tmp_path) -> None:
    turns = build_turns(TurnsFakeRepo(), make_settings(), [], tmp_path)

    async with core_client(turns) as client:
        resp = await client.post(
            "/v1/holds", json=thread_body(text="   "), headers=bearer(CHANNELS_TOKEN)
        )

    assert resp.status_code == 400
    assert resp.json() == {"reason": "bad_request"}


async def test_a_full_hold_refuses_the_next_message(tmp_path) -> None:
    turns = build_turns(TurnsFakeRepo(), make_settings(), [], tmp_path)

    async with core_client(turns) as client:
        for index in range(HOLD_MAX):
            ok = await client.post(
                "/v1/holds", json=thread_body(text=f"m{index}"), headers=bearer(CHANNELS_TOKEN)
            )
            assert ok.status_code == 200
        full = await client.post(
            "/v1/holds", json=thread_body(text="one too many"), headers=bearer(CHANNELS_TOKEN)
        )

    assert full.status_code == 429
    assert full.json() == {"reason": "hold_full"}


def test_a_held_message_past_the_age_limit_is_dropped() -> None:
    now = [1_000.0]
    holds = HoldStore(max_age_s=60.0, clock=lambda: now[0])
    holds.add("thread", "sent last night")
    now[0] += 3_600.0
    holds.add("thread", "sent now")

    assert [m.text for m in holds.list("thread")] == ["sent now"]
    assert [m.text for m in holds.dropped("thread")] == ["sent last night"]
    # The person is told once; the next read has nothing to say.
    assert holds.dropped("thread") == []
    assert [m.text for m in holds.take("thread")] == ["sent now"]


async def test_the_hold_routes_report_what_the_age_limit_dropped(tmp_path) -> None:
    settings = make_settings()
    settings.core.hold_max_age_s = 0.01
    turns = build_turns(TurnsFakeRepo(), settings, [], tmp_path)

    async with core_client(turns) as client:
        await client.post(
            "/v1/holds", json=thread_body(text="all night"), headers=bearer(CHANNELS_TOKEN)
        )
        await asyncio.sleep(0.05)
        taken = await client.post(
            "/v1/holds/take", json=thread_body(), headers=bearer(CHANNELS_TOKEN)
        )

    # The message waited too long: it is not in the text of the next turn, and
    # the answer says that it was dropped.
    assert taken.json()["text"] == ""
    assert [m["text"] for m in taken.json()["dropped"]] == ["all night"]


async def test_holds_need_an_allowed_caller(tmp_path) -> None:
    turns = build_turns(TurnsFakeRepo(), make_settings(), [], tmp_path)

    async with core_client(turns) as client:
        nobody = await client.post("/v1/holds", json=thread_body(text="hi"))
        core = await client.post(
            "/v1/holds", json=thread_body(text="hi"), headers=bearer(CORE_TOKEN)
        )
        laptop = await client.post(
            "/v1/holds", json=thread_body(text="hi"), headers=bearer(LAPTOP_TOKEN)
        )

    assert nobody.status_code == 401
    assert core.status_code == 403
    assert laptop.status_code == 200


# --- the draft of a composer -------------------------------------------------


async def test_the_composer_draft_comes_back_and_a_send_clears_it(tmp_path) -> None:
    turns = build_turns(TurnsFakeRepo(), make_settings(), [], tmp_path)

    async with core_client(turns) as client:
        saved = await client.post(
            "/v1/drafts",
            json=thread_body(text="half a message"),
            headers=bearer(CHANNELS_TOKEN),
        )
        read = await client.get(
            "/v1/drafts",
            params={"channel": "cli:alex", "person": "alex"},
            headers=bearer(CHANNELS_TOKEN),
        )
        cleared = await client.post(
            "/v1/drafts", json=thread_body(text=""), headers=bearer(CHANNELS_TOKEN)
        )
        gone = await client.get(
            "/v1/drafts",
            params={"channel": "cli:alex", "person": "alex"},
            headers=bearer(CHANNELS_TOKEN),
        )

    assert saved.json()["text"] == "half a message"
    assert read.json()["text"] == "half a message"
    assert read.json()["at"]
    # An empty text clears the draft, as a send does.
    assert cleared.json() == {"text": "", "at": None}
    assert gone.json() == {"text": "", "at": None}


async def test_a_draft_is_private_to_its_person(tmp_path) -> None:
    repo = TurnsFakeRepo()
    repo.people["sam"] = Person(id="sam", display_name="Sam", role="member")
    turns = build_turns(repo, make_settings(), [], tmp_path)

    async with core_client(turns) as client:
        for person, text in (("alex", "alex is typing"), ("sam", "sam is typing")):
            await client.post(
                "/v1/drafts",
                json=thread_body(channel="workspace:solar", person=person, text=text),
                headers=bearer(CHANNELS_TOKEN),
            )
        read = {}
        for person in ("alex", "sam"):
            answer = await client.get(
                "/v1/drafts",
                params={"channel": "workspace:solar", "person": person},
                headers=bearer(CHANNELS_TOKEN),
            )
            read[person] = answer.json()["text"]

    # One thread, two people: each composer holds its own draft.
    assert read == {"alex": "alex is typing", "sam": "sam is typing"}


def test_a_draft_past_the_age_limit_is_dropped() -> None:
    now = [1_000.0]
    drafts = DraftStore(max_age_s=60.0, clock=lambda: now[0])
    drafts.save("thread", "written last night")
    now[0] += 3_600.0

    assert drafts.get("thread") is None


async def test_drafts_need_an_allowed_caller(tmp_path) -> None:
    turns = build_turns(TurnsFakeRepo(), make_settings(), [], tmp_path)

    async with core_client(turns) as client:
        nobody = await client.post("/v1/drafts", json=thread_body(text="hi"))
        core = await client.post(
            "/v1/drafts", json=thread_body(text="hi"), headers=bearer(CORE_TOKEN)
        )
        laptop = await client.post(
            "/v1/drafts", json=thread_body(text="hi"), headers=bearer(LAPTOP_TOKEN)
        )

    assert nobody.status_code == 401
    assert core.status_code == 403
    assert laptop.status_code == 200
