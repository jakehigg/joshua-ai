"""Event and event-skill routing tests.

The whole path runs offline: a fake repo, a counting manager (so a test can prove
no model call happened), a fake resolver, and a deliverer that points at a fake
channels app over an ``httpx.ASGITransport``. No DB, no SDK, no network.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import joshua_core.events
import pytest
from fastapi import FastAPI, HTTPException, Request
from joshua_core.delivery import Deliverer
from joshua_core.engine.types import TurnResult
from joshua_core.events import (
    INJECT_FALLBACK,
    SKILL_FRAMING,
    ChannelsResolver,
    EventService,
    ResolvedChannel,
    SkillRegistry,
    append_event_payload,
    build_channels_resolver,
)
from joshua_core.store.models import Channel, Conversation
from joshua_shared.contracts import Attachment, Chat, EventOptions, TurnEvent
from joshua_shared.http import FleetClient


class EventsFakeRepo:
    """In-memory channels, conversations, and transcript writes."""

    def __init__(self) -> None:
        self.channels: dict[str, Channel] = {}
        self.conversations: dict[tuple[str, str | None], Conversation] = {}
        self.transcripts: list[dict[str, Any]] = []
        self._next = 0

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

    async def resolve_channel(self, ref: str) -> Channel | None:
        return self.channels.get(ref)

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


class CountingManager:
    """Counts ``run_turn`` calls and returns a canned reply."""

    def __init__(self, reply: str = "(reply)") -> None:
        self.calls: list[SimpleNamespace] = []
        self._reply = reply

    async def run_turn(
        self, channel: Channel, conversation: Conversation, text: str, **kwargs: Any
    ) -> TurnResult:
        self.calls.append(
            SimpleNamespace(channel=channel, conversation=conversation, text=text, **kwargs)
        )
        return TurnResult(
            text=self._reply, session_id="s-1", is_error=False, usage={}, cost_usd=0.0
        )


def make_deliverer(received: list[dict[str, Any]]) -> Deliverer:
    app = FastAPI()

    @app.post("/v1/deliver")
    async def deliver(request: Request) -> dict[str, bool]:
        received.append(await request.json())
        return {"ok": True}

    client = FleetClient("http://channels", "core-tok", transport=httpx.ASGITransport(app=app))
    return Deliverer(client)


def static_resolver(mapping: dict[str, ResolvedChannel]):
    async def resolve(ref: str) -> ResolvedChannel | None:
        return mapping.get(ref)

    return resolve


def write_skill(prompts_dir: Path, event_type: str, body: str) -> None:
    skills = prompts_dir / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / f"{event_type}.md").write_text(body)


def build_service(
    tmp_path: Path,
    *,
    repo: EventsFakeRepo | None = None,
    manager: CountingManager | None = None,
    received: list[dict[str, Any]] | None = None,
    resolver: Any = None,
) -> tuple[EventService, EventsFakeRepo, CountingManager, list[dict[str, Any]]]:
    repo = repo or EventsFakeRepo()
    manager = manager or CountingManager()
    received = received if received is not None else []
    resolver = resolver or static_resolver({})
    service = EventService(
        repo=repo,
        registry=SkillRegistry(tmp_path),
        resolver=resolver,
        manager=manager,
        deliverer=make_deliverer(received),
        prompts_dir=tmp_path,
    )
    return service, repo, manager, received


def named_event(event_type: str = "motion", **overrides: Any) -> TurnEvent:
    data: dict[str, Any] = {
        "channel": "",
        "chat": Chat(kind="dm", id="event"),
        "kind": "event",
        "event_type": event_type,
        "payload": {"room": "kitchen"},
    }
    data.update(overrides)
    return TurnEvent(**data)


# --- named events ----------------------------------------------------------


async def test_unknown_event_type_is_ignored(tmp_path) -> None:
    service, _repo, manager, received = build_service(tmp_path)
    resp = await service.handle(named_event("nope"))
    await service.drain()

    assert resp.status_code == 200
    assert resp.body == b'{"ignored":true}'
    assert manager.calls == []
    assert received == []


async def test_named_event_runs_skill_and_delivers_to_destination(tmp_path) -> None:
    write_skill(tmp_path, "motion", "---\ndestination: everyone\n---\nReport the motion.")
    resolver = static_resolver(
        {"everyone": ResolvedChannel(channel_id="telegram:-100", chat_kind="group")}
    )
    service, repo, manager, received = build_service(tmp_path, resolver=resolver)

    resp = await service.handle(named_event("motion"))
    assert resp.status_code == 202
    await service.drain()

    assert len(manager.calls) == 1
    call = manager.calls[0]
    assert call.framing == SKILL_FRAMING
    assert call.direction == "inject"
    assert "Report the motion." in call.text
    assert '"room": "kitchen"' in call.text  # payload appended as a JSON block
    # The destination channel is created on first use, group -> shared session.
    assert repo.channels["telegram:-100"].session_mode == "shared"
    assert received == [{"channel": "everyone", "text": "(reply)", "attachments": []}]


async def test_skill_frontmatter_overrides_framing(tmp_path) -> None:
    write_skill(
        tmp_path, "digest", "---\ndestination: everyone\nframing: Custom framing.\n---\nBody."
    )
    resolver = static_resolver(
        {"everyone": ResolvedChannel(channel_id="telegram:1", chat_kind="dm")}
    )
    service, _repo, manager, _received = build_service(tmp_path, resolver=resolver)

    await service.handle(named_event("digest"))
    await service.drain()

    assert manager.calls[0].framing == "Custom framing."


async def test_named_event_unresolved_destination_is_ignored(tmp_path) -> None:
    write_skill(tmp_path, "motion", "---\ndestination: nowhere\n---\nBody.")
    service, _repo, manager, received = build_service(tmp_path)  # empty resolver

    resp = await service.handle(named_event("motion"))
    await service.drain()

    assert resp.status_code == 200
    assert b"unresolved_destination" in resp.body
    assert manager.calls == []
    assert received == []


async def test_ignore_reply_is_not_delivered(tmp_path) -> None:
    write_skill(tmp_path, "quiet", "---\ndestination: everyone\n---\nMaybe stay silent.")
    resolver = static_resolver(
        {"everyone": ResolvedChannel(channel_id="telegram:1", chat_kind="dm")}
    )
    manager = CountingManager(reply="[IGNORE]")
    service, _repo, _mgr, received = build_service(tmp_path, manager=manager, resolver=resolver)

    await service.handle(named_event("quiet"))
    await service.drain()

    assert received == []


# --- channel-addressed events ---------------------------------------------


def addressed_event(**overrides: Any) -> TurnEvent:
    data: dict[str, Any] = {
        "channel": "telegram:998877",
        "chat": Chat(kind="dm", id="998877"),
        "kind": "event",
        "text": "the garage door is open",
    }
    data.update(overrides)
    return TurnEvent(**data)


async def test_verbatim_delivers_input_with_no_model_call(tmp_path) -> None:
    repo = EventsFakeRepo()
    repo.channels["telegram:998877"] = Channel(
        id="telegram:998877", channel_type="telegram", session_mode="per_person"
    )
    service, repo, manager, received = build_service(tmp_path, repo=repo)

    resp = await service.handle(addressed_event(options=EventOptions(verbatim=True)))
    await service.drain()

    assert resp.status_code == 200
    assert manager.calls == []  # no model call
    inject_rows = [r for r in repo.transcripts if r["direction"] == "inject"]
    assert len(inject_rows) == 1
    assert inject_rows[0]["content"] == "the garage door is open"
    assert received == []  # channels already delivered; core only records


async def test_channel_addressed_without_verbatim_runs_inject_framing(tmp_path) -> None:
    repo = EventsFakeRepo()
    repo.channels["telegram:998877"] = Channel(
        id="telegram:998877", channel_type="telegram", session_mode="per_person"
    )
    service, repo, manager, received = build_service(tmp_path, repo=repo)

    attach = Attachment(path="attachments/2026/08/x.jpg", mime="image/jpeg")
    resp = await service.handle(addressed_event(attachments=[attach]))
    assert resp.status_code == 202
    await service.drain()

    assert len(manager.calls) == 1
    call = manager.calls[0]
    assert call.direction == "inject"
    assert "webhook event" in call.framing
    assert call.text == "the garage door is open"
    assert call.attachments[0].path == "attachments/2026/08/x.jpg"
    assert received == [{"channel": "telegram:998877", "text": "(reply)", "attachments": []}]


async def test_channel_addressed_unknown_channel_is_ignored(tmp_path) -> None:
    service, _repo, manager, received = build_service(tmp_path)  # no channels

    resp = await service.handle(addressed_event(channel="telegram:404"))
    await service.drain()

    assert resp.status_code == 200
    assert b"unknown_channel" in resp.body
    assert manager.calls == []
    assert received == []


# --- units -----------------------------------------------------------------


def test_append_event_payload() -> None:
    assert append_event_payload("prompt", None) == "prompt"
    assert append_event_payload("prompt", {}) == "prompt"
    out = append_event_payload("prompt", {"b": 1, "a": 2})
    assert out.startswith("prompt\n\n```json\n")
    assert '"a": 2' in out and '"b": 1' in out


def test_registry_rejects_bad_slug_and_missing_destination(tmp_path) -> None:
    registry = SkillRegistry(tmp_path)
    assert registry.get("../secret") is None  # path traversal blocked
    assert registry.get("Bad Name") is None
    assert registry.get("absent") is None  # no file

    write_skill(tmp_path, "nodest", "---\nframing: x\n---\nBody.")
    assert registry.get("nodest") is None  # no destination -> ignored


def test_registry_reads_frontmatter(tmp_path) -> None:
    write_skill(tmp_path, "ok", '---\ndestination: "everyone"\n---\nThe body.')
    skill = SkillRegistry(tmp_path).get("ok")
    assert skill is not None
    assert skill.destination == "everyone"
    assert skill.prompt == "The body."
    assert skill.framing is None


def test_registry_reads_body_without_frontmatter(tmp_path) -> None:
    # A file with no frontmatter has no destination, so it is ignored.
    write_skill(tmp_path, "plain", "Just a body, no frontmatter.")
    assert SkillRegistry(tmp_path).get("plain") is None


def test_inject_fallback_used_when_event_file_missing(tmp_path) -> None:
    # prompts_dir has no builtin/event.md, so the fallback framing is used.
    service, _repo, _mgr, _received = build_service(tmp_path)
    assert service._inject_framing() == INJECT_FALLBACK


def test_inject_framing_reads_event_file(tmp_path) -> None:
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    (builtin / "event.md").write_text("Framed by the file.\n")
    service, _repo, _mgr, _received = build_service(tmp_path)
    assert service._inject_framing() == "Framed by the file."


async def test_event_turn_failure_is_swallowed(tmp_path) -> None:
    class BoomManager:
        async def run_turn(self, *args: Any, **kwargs: Any) -> TurnResult:
            raise RuntimeError("boom")

    repo = EventsFakeRepo()
    repo.channels["telegram:998877"] = Channel(
        id="telegram:998877", channel_type="telegram", session_mode="per_person"
    )
    received: list[dict[str, Any]] = []
    service = EventService(
        repo=repo,
        registry=SkillRegistry(tmp_path),
        resolver=static_resolver({}),
        manager=BoomManager(),
        deliverer=make_deliverer(received),
        prompts_dir=tmp_path,
    )

    resp = await service.handle(addressed_event())
    assert resp.status_code == 202
    await service.drain()  # must not raise
    assert received == []


def test_build_channels_resolver() -> None:
    with pytest.raises(RuntimeError):
        build_channels_resolver({})
    resolver = build_channels_resolver(
        {"CHANNELS_URL": "http://channels", "JOSHUA_TOKEN_CORE": "t"}
    )
    assert isinstance(resolver, ChannelsResolver)


async def test_channels_resolver_missing_channel_id_returns_none() -> None:
    app = FastAPI()

    @app.get("/v1/channels/resolve")
    async def resolve(ref: str) -> dict[str, str]:
        return {}

    client = FleetClient("http://channels", "core-tok", transport=httpx.ASGITransport(app=app))
    assert await ChannelsResolver(client).resolve("everyone") is None


async def test_event_kind_without_service_is_501() -> None:
    from joshua_core.turns import TurnService

    turns = TurnService(
        repo=EventsFakeRepo(),
        settings=SimpleNamespace(),
        manager=CountingManager(),
        deliverer=make_deliverer([]),
        events=None,
    )
    resp = await turns.accept(addressed_event())
    assert resp.status_code == 501


async def test_channels_resolver_over_http() -> None:
    app = FastAPI()

    @app.get("/v1/channels/resolve")
    async def resolve(ref: str) -> dict[str, str]:
        if ref == "everyone":
            return {"channel_id": "telegram:-100", "chat_kind": "group"}
        raise HTTPException(status_code=404)

    client = FleetClient("http://channels", "core-tok", transport=httpx.ASGITransport(app=app))
    resolver = ChannelsResolver(client)

    hit = await resolver.resolve("everyone")
    assert hit == ResolvedChannel(channel_id="telegram:-100", chat_kind="group")
    assert await resolver.resolve("missing") is None


def test_skill_registry_default_resolves_to_the_packaged_prompts() -> None:
    """``SkillRegistry(None)`` must resolve, because ``core.prompts_dir`` defaults
    to None. It used to raise, so it was the one caller that blocked that default."""
    from joshua_core.events import SkillRegistry

    registry = SkillRegistry(None)
    expected = Path(joshua_core.events.__file__).parent / "prompts" / "skills"
    assert registry._dir == expected
    assert registry.get("anything") is None
