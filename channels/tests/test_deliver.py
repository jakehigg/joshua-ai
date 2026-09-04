"""Deliver and resolve route tests: auth, resolution, attachments, and send."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from adapter_fakes import FakeAdapter
from joshua_channels.app import build_app
from joshua_channels.deliver import ChannelsContext
from joshua_channels.registry import AdapterRegistry
from joshua_shared import config as config_module

CORE_TOKEN = "core-tok"
CHANNELS_TOKEN = "chan-tok"
LAPTOP_TOKEN = "laptop-tok"

GROUP_CHAT = "iMessage;+;chat100000000000000001"

CONFIG = f"""
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    handles:
      imessage: "+15551234567"
      telegram: "998877"
groups:
  - id: everyone
    channel: imessage
    chat_id: "{GROUP_CHAT}"
channels:
  destinations:
    everyone: imessage:group:everyone
"""


@pytest.fixture(autouse=True)
def _fleet_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE_TOKEN)
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", CHANNELS_TOKEN)
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP_TOKEN)


def _settings():
    return config_module.parse(CONFIG, env={}, source="<test>")


def _make(*, fail: bool = False) -> tuple[Any, FakeAdapter, FakeAdapter]:
    telegram = FakeAdapter("telegram", {"dm:alex": "998877"}, fail=fail)
    imessage = FakeAdapter("imessage", {"group:everyone": GROUP_CHAT}, fail=fail)
    registry = AdapterRegistry()
    registry.register(telegram)
    registry.register(imessage)
    context = ChannelsContext(settings=_settings(), registry=registry, data_dir="/data")
    app = build_app(context)
    return app, telegram, imessage


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://channels")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_literal_id_sends_once() -> None:
    app, telegram, _ = _make()
    async with _client(app) as client:
        resp = await client.post(
            "/v1/deliver",
            json={"channel": "telegram:998877", "text": "hello"},
            headers=_bearer(CORE_TOKEN),
        )
    assert resp.status_code == 200
    assert resp.json() == {"delivered": True, "parts": 1}
    assert telegram.sent == [("998877", "hello", [])]


async def test_non_core_identity_is_forbidden() -> None:
    app, _, _ = _make()
    async with _client(app) as client:
        for token in (CHANNELS_TOKEN, LAPTOP_TOKEN):
            resp = await client.post(
                "/v1/deliver",
                json={"channel": "telegram:998877", "text": "hi"},
                headers=_bearer(token),
            )
            assert resp.status_code == 403


async def test_missing_bearer_is_401() -> None:
    app, _, _ = _make()
    async with _client(app) as client:
        resp = await client.post("/v1/deliver", json={"channel": "telegram:998877", "text": "hi"})
    assert resp.status_code == 401


async def test_unknown_channel_is_404() -> None:
    app, _, _ = _make()
    async with _client(app) as client:
        resp = await client.post(
            "/v1/deliver",
            json={"channel": "telegram:dm:ghost", "text": "hi"},
            headers=_bearer(CORE_TOKEN),
        )
    assert resp.status_code == 404
    assert resp.json() == {"reason": "unknown_channel"}


async def test_group_accepts_wiki_attachment() -> None:
    app, _, imessage = _make()
    async with _client(app) as client:
        resp = await client.post(
            "/v1/deliver",
            json={"channel": "everyone", "text": "hi", "attachments": ["wiki/x.md"]},
            headers=_bearer(CORE_TOKEN),
        )
    assert resp.status_code == 200
    assert imessage.sent[0][2] == [Path("/data/wiki/x.md")]


async def test_group_rejects_personal_attachment() -> None:
    app, _, imessage = _make()
    async with _client(app) as client:
        resp = await client.post(
            "/v1/deliver",
            json={"channel": "everyone", "text": "hi", "attachments": ["journal/x.md"]},
            headers=_bearer(CORE_TOKEN),
        )
    assert resp.status_code == 400
    assert resp.json()["reason"] == "invalid_attachment"
    assert imessage.sent == []


async def test_group_accepts_shared_attachment() -> None:
    app, _, imessage = _make()
    async with _client(app) as client:
        resp = await client.post(
            "/v1/deliver",
            json={"channel": "everyone", "text": "report", "attachments": ["shared/r.pdf"]},
            headers=_bearer(CORE_TOKEN),
        )
    assert resp.status_code == 200
    assert imessage.sent[0][2] == [Path("/data/shared/r.pdf")]


async def test_dm_maps_person_attachment() -> None:
    app, telegram, _ = _make()
    async with _client(app) as client:
        resp = await client.post(
            "/v1/deliver",
            json={"channel": "telegram:dm:alex", "text": "note", "attachments": ["journal/x.md"]},
            headers=_bearer(CORE_TOKEN),
        )
    assert resp.status_code == 200
    assert telegram.sent[0][0] == "998877"
    assert telegram.sent[0][2] == [Path("/data/people/alex/journal/x.md")]


async def test_unsafe_attachment_is_rejected() -> None:
    app, telegram, _ = _make()
    async with _client(app) as client:
        resp = await client.post(
            "/v1/deliver",
            json={"channel": "telegram:dm:alex", "text": "x", "attachments": ["../secrets"]},
            headers=_bearer(CORE_TOKEN),
        )
    assert resp.status_code == 400
    assert telegram.sent == []


async def test_adapter_error_is_502() -> None:
    app, _, _ = _make(fail=True)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/deliver",
            json={"channel": "telegram:998877", "text": "hi"},
            headers=_bearer(CORE_TOKEN),
        )
    assert resp.status_code == 502
    assert resp.json()["reason"] == "send_failed"


async def test_bad_body_is_400() -> None:
    app, _, _ = _make()
    async with _client(app) as client:
        resp = await client.post("/v1/deliver", content=b"not json", headers=_bearer(CORE_TOKEN))
    assert resp.status_code == 400


async def test_resolve_returns_channel_and_chat() -> None:
    app, _, _ = _make()
    async with _client(app) as client:
        resp = await client.get(
            "/v1/channels/resolve", params={"ref": "everyone"}, headers=_bearer(CORE_TOKEN)
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel"] == f"imessage:{GROUP_CHAT}"
    assert body["chat"] == {"kind": "group", "id": GROUP_CHAT, "title": "everyone"}
    # Flat aliases the core resolver reads.
    assert body["channel_id"] == f"imessage:{GROUP_CHAT}"
    assert body["chat_kind"] == "group"


async def test_resolve_unknown_is_404() -> None:
    app, _, _ = _make()
    async with _client(app) as client:
        resp = await client.get(
            "/v1/channels/resolve", params={"ref": "nope"}, headers=_bearer(CORE_TOKEN)
        )
    assert resp.status_code == 404


async def test_resolve_requires_core_identity() -> None:
    app, _, _ = _make()
    async with _client(app) as client:
        resp = await client.get(
            "/v1/channels/resolve", params={"ref": "everyone"}, headers=_bearer(LAPTOP_TOKEN)
        )
    assert resp.status_code == 403
