"""End-to-end smoke suite for the core kernel against a real Postgres.

The whole core runs with ``AGENT_BACKEND=stub``: the real store, routing,
events, scheduler, and admin surface, but no SDK, token, or network. Channels is
a fake app over an httpx ``ASGITransport`` that records deliveries. Each check
mirrors one row of the ``make smoke`` acceptance list, so phase 3+ cannot
regress the kernel silently.

Marked ``integration`` — CI runs it against the pgvector service, and ``make
smoke`` runs it against the compose Postgres.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import joshua_core
import pytest
from fastapi import FastAPI, Request
from joshua_core.delivery import Deliverer
from joshua_core.engine.cron import now_in
from joshua_core.events import EventService, ResolvedChannel, SkillRegistry
from joshua_core.main import AppContext, _build_manager, build_app
from joshua_core.memory.indexer import Indexer
from joshua_core.memory.nightly import NightlyReflector
from joshua_core.memory.sources.files import FilesSource
from joshua_core.memory.store import MemoryStore
from joshua_core.scheduler import Scheduler
from joshua_core.store.seed import seed_people
from joshua_core.turns import TurnService
from joshua_shared import config as config_module
from joshua_shared.http import FleetClient

pytestmark = pytest.mark.integration

CHANNELS_TOKEN = "chan-tok"
CORE_TOKEN = "core-tok"
LAPTOP_TOKEN = "laptop-tok"

TZ = "America/New_York"
DM_CHANNEL = "telegram:998877"
GROUP_CHANNEL = "telegram:-100"
PACKAGED_PROMPTS = Path(joshua_core.__file__).parent / "prompts"

CONFIG = """
name: Smoke House
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


def _make_fake_channels(received: list[dict[str, Any]]) -> Any:
    app = FastAPI()

    @app.post("/v1/deliver")
    async def deliver(request: Request) -> dict[str, bool]:
        received.append(await request.json())
        return {"ok": True}

    return app


async def _resolve_everyone(name: str) -> ResolvedChannel | None:
    """Resolve the one destination the skill test uses to the group channel."""
    if name == "everyone":
        return ResolvedChannel(channel_id=GROUP_CHANNEL, chat_kind="group")
    return None


@pytest.fixture
async def wired(repo, tmp_path, monkeypatch):
    """A fully wired core over the real repo, the stub backend, and fake channels."""
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", CHANNELS_TOKEN)
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE_TOKEN)
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP_TOKEN)
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    monkeypatch.delenv("GATEWAY_URL", raising=False)

    settings = config_module.parse(CONFIG, env={}, source="<smoke>")
    settings.core.prompts_dir = str(PACKAGED_PROMPTS)
    await seed_people(repo, settings)

    received: list[dict[str, Any]] = []
    client = FleetClient(
        "http://channels",
        CORE_TOKEN,
        transport=httpx.ASGITransport(app=_make_fake_channels(received)),
    )
    deliverer = Deliverer(client)

    manager = _build_manager(settings, repo, "stub")

    skills_dir = tmp_path / "prompts"
    (skills_dir / "skills").mkdir(parents=True)
    (skills_dir / "skills" / "doorbell.md").write_text(
        "---\ndestination: everyone\n---\nSomeone is at the door.\n"
    )
    events = EventService(
        repo=repo,
        registry=SkillRegistry(skills_dir),
        resolver=_resolve_everyone,
        manager=manager,
        deliverer=deliverer,
        prompts_dir=skills_dir,
    )
    turns = TurnService(
        repo=repo, settings=settings, manager=manager, deliverer=deliverer, events=events
    )
    scheduler = Scheduler(repo=repo, manager=manager, deliverer=deliverer, tz=TZ)

    memory_store = MemoryStore(repo._pool)
    indexer = Indexer(
        memory_store,
        {"files": FilesSource(tmp_path / "data")},
        embed_model=settings.memory.embed_model,
        chunk_chars=settings.memory.chunk_chars,
    )
    reflector = NightlyReflector(
        repo=repo, indexer=indexer, manager=manager, settings=settings, data_dir=tmp_path
    )

    app = build_app()
    app.state.ctx = AppContext(
        settings=settings,
        repo=repo,
        agent_backend="stub",
        manager=manager,
        deliverer=deliverer,
        turns=turns,
        scheduler=scheduler,
        memory=memory_store,
        indexer=indexer,
        reflector=reflector,
    )
    http_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://core")
    try:
        yield SimpleNamespace(
            client=http_client,
            repo=repo,
            received=received,
            turns=turns,
            scheduler=scheduler,
            manager=manager,
        )
    finally:
        await http_client.aclose()
        await client.aclose()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _dm_event(**overrides: Any) -> dict[str, Any]:
    event = {
        "channel": DM_CHANNEL,
        "chat": {"kind": "dm", "id": "998877"},
        "handle": {"type": "telegram", "id": "998877"},
        "text": "hello there",
        "message_id": "m-1",
    }
    event.update(overrides)
    return event


async def test_schema_applied_and_people_seeded(wired) -> None:
    # The schema is applied by the db fixture; seeding wrote the roster cache.
    people = await wired.repo.list_people()
    assert [p.id for p in people] == ["alex"]
    assert await wired.repo.get_person_by_handle("telegram", "998877") is not None


async def test_dm_turn_delivers_stub_reply(wired) -> None:
    resp = await wired.client.post("/v1/turns", json=_dm_event(), headers=_bearer(CHANNELS_TOKEN))
    assert resp.status_code == 202
    await wired.turns.drain()

    assert len(wired.received) == 1
    assert wired.received[0]["channel"] == DM_CHANNEL
    assert wired.received[0]["text"].startswith("(stub)")


async def test_group_turn_is_speaker_labelled(wired) -> None:
    event = {
        "channel": GROUP_CHANNEL,
        "chat": {"kind": "group", "id": "-100", "title": "Everyone"},
        "handle": {"type": "telegram", "id": "998877"},
        "text": "hi all",
        "message_id": "g-1",
    }
    resp = await wired.client.post("/v1/turns", json=event, headers=_bearer(CHANNELS_TOKEN))
    assert resp.status_code == 202
    await wired.turns.drain()

    conv = await wired.repo.get_or_create_conversation(GROUP_CHANNEL, None)
    rows = await wired.repo.transcript_tail(conv.id)
    in_row = next(r for r in rows if r["direction"] == "in")
    assert in_row["meta"]["speaker"] == "Alex"


async def test_unknown_handle_is_403(wired) -> None:
    event = _dm_event(
        channel="telegram:404",
        chat={"kind": "dm", "id": "404"},
        handle={"type": "telegram", "id": "000"},
    )
    resp = await wired.client.post("/v1/turns", json=event, headers=_bearer(CHANNELS_TOKEN))
    assert resp.status_code == 403
    assert resp.json() == {"reason": "unknown_sender"}


async def test_duplicate_message_id_not_rerun(wired) -> None:
    first = await wired.client.post("/v1/turns", json=_dm_event(), headers=_bearer(CHANNELS_TOKEN))
    await wired.turns.drain()
    second = await wired.client.post("/v1/turns", json=_dm_event(), headers=_bearer(CHANNELS_TOKEN))
    await wired.turns.drain()

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {"accepted": False, "reason": "duplicate"}
    assert len(wired.received) == 1


async def test_scheduled_task_fires(wired) -> None:
    await wired.repo.upsert_channel(DM_CHANNEL, "telegram", session_mode="per_person")
    conv = await wired.repo.get_or_create_conversation(DM_CHANNEL, "alex")
    task = await wired.repo.create_task(conv.id, "remind me", now_in(TZ) - timedelta(seconds=1))

    await wired.scheduler._sweep()

    assert wired.received and wired.received[0]["channel"] == DM_CHANNEL
    assert (await wired.repo.get_task(task.id)).status == "completed"


async def test_event_unknown_type_is_ignored(wired) -> None:
    event = {
        "channel": "n/a",
        "chat": {"kind": "dm", "id": "n/a"},
        "kind": "event",
        "event_type": "no_such_skill",
    }
    resp = await wired.client.post("/v1/turns", json=event, headers=_bearer(CHANNELS_TOKEN))
    assert resp.status_code == 200
    assert resp.json()["ignored"] is True


async def test_event_skill_runs_and_delivers(wired) -> None:
    event = {
        "channel": "n/a",
        "chat": {"kind": "dm", "id": "n/a"},
        "kind": "event",
        "event_type": "doorbell",
        "payload": {"who": "courier"},
    }
    resp = await wired.client.post("/v1/turns", json=event, headers=_bearer(CHANNELS_TOKEN))
    assert resp.status_code == 202
    await wired.turns.drain()

    assert wired.received and wired.received[0]["channel"] == "everyone"
    assert wired.received[0]["text"].startswith("(stub)")


async def test_event_verbatim_is_recorded_not_delivered(wired) -> None:
    # Channels delivers the verbatim text; core only records it .
    await wired.repo.upsert_channel(GROUP_CHANNEL, "telegram", session_mode="shared")
    event = {
        "channel": GROUP_CHANNEL,
        "chat": {"kind": "group", "id": "-100"},
        "kind": "event",
        "text": "Garage door left open",
        "options": {"verbatim": True},
    }
    resp = await wired.client.post("/v1/turns", json=event, headers=_bearer(CHANNELS_TOKEN))
    assert resp.status_code == 200
    assert resp.json()["verbatim"] is True

    assert wired.received == []
    conv = await wired.repo.get_or_create_conversation(GROUP_CHANNEL, None)
    rows = await wired.repo.transcript_tail(conv.id)
    inject_rows = [r for r in rows if r["direction"] == "inject"]
    assert len(inject_rows) == 1
    assert inject_rows[0]["content"] == "Garage door left open"


async def test_pool_flush_drops_warm_sessions(wired) -> None:
    await wired.client.post("/v1/turns", json=_dm_event(), headers=_bearer(CHANNELS_TOKEN))
    await wired.turns.drain()

    resp = await wired.client.post("/admin/sessions/flush", headers=_bearer(LAPTOP_TOKEN))
    assert resp.status_code == 200
    assert resp.json()["flushed"] >= 1


async def test_admin_pool_view(wired) -> None:
    await wired.client.post("/v1/turns", json=_dm_event(), headers=_bearer(CHANNELS_TOKEN))
    await wired.turns.drain()

    resp = await wired.client.get("/admin/pool", headers=_bearer(LAPTOP_TOKEN))
    assert resp.status_code == 200
    body = resp.json()
    assert body["max"] == 20
    session = next(s for s in body["sessions"] if s["person_id"] == "alex")
    assert session["profile"] == "dm"
    assert session["locked"] is False


async def test_admin_turn_returns_text_without_delivering(wired) -> None:
    await wired.repo.upsert_channel(
        DM_CHANNEL, "telegram", session_mode="per_person", default_person_id="alex"
    )
    resp = await wired.client.post(
        "/admin/turn",
        json={"channel": DM_CHANNEL, "person": "alex", "text": "ping"},
        headers=_bearer(LAPTOP_TOKEN),
    )
    assert resp.status_code == 200
    assert resp.json()["text"].startswith("(stub)")
    # DELIVER_DRY_RUN semantics: the operator turn never reaches a channel.
    assert wired.received == []


async def test_admin_transcript_tail(wired) -> None:
    await wired.client.post("/v1/turns", json=_dm_event(), headers=_bearer(CHANNELS_TOKEN))
    await wired.turns.drain()
    conv = await wired.repo.get_or_create_conversation(DM_CHANNEL, "alex")

    resp = await wired.client.get(
        f"/admin/transcript/{conv.id}?limit=5", headers=_bearer(LAPTOP_TOKEN)
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert [r["direction"] for r in rows] == ["in", "out"]
    assert isinstance(rows[0]["created_at"], str)


async def test_admin_auth_forbids_core_identity(wired) -> None:
    # core authenticates but is not in ADMIN_CALLERS (default laptop,ci).
    for path in ("/admin/pool", "/admin/people"):
        resp = await wired.client.get(path, headers=_bearer(CORE_TOKEN))
        assert resp.status_code == 403
    missing = await wired.client.get("/admin/pool")
    assert missing.status_code == 401
