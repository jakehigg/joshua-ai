"""Guard admin route tests: auth gating, stats, and the recent ring."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from joshua_channels.admin import build_admin_router
from joshua_channels.app import build_app
from joshua_channels.deliver import ChannelsContext
from joshua_channels.guard import Guard
from joshua_channels.registry import AdapterRegistry
from joshua_shared import config as config_module

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
channels:
  limits:
    per_handle_per_minute: 20
    max_text_chars: 8000
"""


@pytest.fixture(autouse=True)
def _fleet_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE_TOKEN)
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP_TOKEN)
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)


def _settings():
    return config_module.parse(CONFIG, env={}, source="<test>")


def _app_with_guard() -> tuple[Any, Guard]:
    settings = _settings()
    guard = Guard(settings, settings.channels.limits)
    context = ChannelsContext(settings=settings, registry=AdapterRegistry(), guard=guard)
    return build_app(context), guard


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://channels")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_router_serves_the_admin_routes() -> None:
    paths = {route.path for route in build_admin_router().routes}
    assert paths == {
        "/admin/guard/stats",
        "/admin/guard/recent",
        "/admin/chats/unconfigured",
    }


async def test_stats_requires_admin_caller() -> None:
    app, _ = _app_with_guard()
    async with _client(app) as client:
        assert (await client.get("/admin/guard/stats")).status_code == 401
        # core is a fleet identity but not in ADMIN_CALLERS.
        resp = await client.get("/admin/guard/stats", headers=_bearer(CORE_TOKEN))
        assert resp.status_code == 403


async def test_stats_reports_counts() -> None:
    app, guard = _app_with_guard()
    guard.check(
        channel_type="telegram",
        sender_handle="555",
        chat_id="c",
        chat_kind="dm",
        text_len=1,
        attachment_bytes=0,
    )
    async with _client(app) as client:
        resp = await client.get("/admin/guard/stats", headers=_bearer(LAPTOP_TOKEN))
    assert resp.status_code == 200
    body = resp.json()
    assert body["unknown_sender"] == 1
    assert body["refused_total"] == 1


async def test_recent_lists_refusal_address() -> None:
    app, guard = _app_with_guard()
    guard.check(
        channel_type="telegram",
        sender_handle="555",
        chat_id="c",
        chat_kind="dm",
        text_len=1,
        attachment_bytes=0,
    )
    async with _client(app) as client:
        resp = await client.get("/admin/guard/recent", headers=_bearer(LAPTOP_TOKEN))
    assert resp.status_code == 200
    recent = resp.json()["recent"]
    assert recent[0]["address"] == "555"


async def test_stats_without_guard_is_zeroed() -> None:
    settings = _settings()
    context = ChannelsContext(settings=settings, registry=AdapterRegistry())
    app = build_app(context)
    async with _client(app) as client:
        resp = await client.get("/admin/guard/stats", headers=_bearer(LAPTOP_TOKEN))
    assert resp.status_code == 200
    assert resp.json()["refused_total"] == 0


async def test_unconfigured_requires_admin_caller() -> None:
    app, _ = _app_with_guard()
    async with _client(app) as client:
        assert (await client.get("/admin/chats/unconfigured")).status_code == 401
        resp = await client.get("/admin/chats/unconfigured", headers=_bearer(CORE_TOKEN))
        assert resp.status_code == 403


async def test_unconfigured_lists_a_group_the_config_does_not_hold() -> None:
    app, guard = _app_with_guard()
    guard.check(
        channel_type="telegram",
        sender_handle="998877",
        chat_id="-100777",
        chat_kind="group",
        chat_title="Family",
        text_len=1,
        attachment_bytes=0,
    )
    async with _client(app) as client:
        resp = await client.get("/admin/chats/unconfigured", headers=_bearer(LAPTOP_TOKEN))
        assert resp.status_code == 200
        groups = resp.json()["groups"]
        assert [g["chat_id"] for g in groups] == ["-100777"]
        assert groups[0]["chat_title"] == "Family"
