"""The terminal channel: guard, streaming, and the outbox.

The terminal is a channel, so an unknown person is refused at channels and never
reaches core. These tests run the real routes over ``ASGITransport``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from joshua_channels.adapters.cli import CliAdapter, outbox_path
from joshua_channels.app import build_app
from joshua_channels.cli_routes import build_cli_router
from joshua_channels.deliver import ChannelsContext
from joshua_channels.guard import Guard
from joshua_channels.registry import AdapterRegistry
from joshua_shared import config as config_module

LAPTOP_TOKEN = "laptop-tok"
CORE_TOKEN = "core-tok"

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    handles:
      telegram: "998877"
  - id: gwen
    name: Gwen
    role: guest
"""


class FakeStreamCore:
    """A core that streams two deltas and a done frame."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def stream_turn(self, event: Any):
        self.events.append(event)
        yield "delta", {"text": "hel"}
        yield "delta", {"text": "lo"}
        yield "done", {"turn_id": "t1", "text": "hello", "tools": []}


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP_TOKEN)
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE_TOKEN)


def _make(tmp_path: Path, *, core: Any = None) -> tuple[Any, Any]:
    core = core or FakeStreamCore()
    settings = config_module.parse(CONFIG, env={}, source="<test>")
    registry = AdapterRegistry()
    registry.register(CliAdapter(data_dir=tmp_path))
    context = ChannelsContext(
        settings=settings,
        registry=registry,
        data_dir=str(tmp_path),
        guard=Guard(settings, settings.channels.limits, config_provider=lambda: settings),
        core_client=core,
    )
    return build_app(context), core


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://channels")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_the_router_serves_both_routes() -> None:
    paths = {r.path for r in build_cli_router().routes}
    assert paths == {"/v1/cli/turns/stream", "/v1/cli/outbox"}


# --- inbound ---------------------------------------------------------------


async def test_a_known_person_gets_the_reply(tmp_path: Path) -> None:
    app, core = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            "/v1/cli/turns/stream",
            json={"person": "alex", "text": "hello"},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert response.status_code == 200
    assert "hello" in response.text
    assert core.events[0].handle.type == "cli"
    assert core.events[0].channel == "cli:alex"


async def test_an_unknown_person_never_reaches_core(tmp_path: Path) -> None:
    """The guard runs here, so a bad person id stops at channels."""
    app, core = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            "/v1/cli/turns/stream",
            json={"person": "nobody", "text": "hello"},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert response.status_code == 403
    assert core.events == []


async def test_a_guest_may_use_the_terminal(tmp_path: Path) -> None:
    app, core = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            "/v1/cli/turns/stream",
            json={"person": "gwen", "text": "hello"},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert response.status_code == 200
    assert core.events[0].channel == "cli:gwen"


async def test_no_bearer_is_refused(tmp_path: Path) -> None:
    app, core = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            "/v1/cli/turns/stream", json={"person": "alex", "text": "hello"}
        )
    assert response.status_code == 401
    assert core.events == []


async def test_an_empty_message_is_a_bad_request(tmp_path: Path) -> None:
    app, _ = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            "/v1/cli/turns/stream",
            json={"person": "alex", "text": "  "},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert response.status_code == 400


# --- outbound --------------------------------------------------------------


async def test_send_appends_to_the_outbox(tmp_path: Path) -> None:
    adapter = CliAdapter(data_dir=tmp_path)
    await adapter.send("alex", "your reminder", [])
    lines = outbox_path(tmp_path, "alex").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "your reminder"


def test_resolve_ref_takes_a_dm(tmp_path: Path) -> None:
    adapter = CliAdapter(data_dir=tmp_path)
    assert adapter.resolve_ref("dm:alex") == "alex"
    assert adapter.resolve_ref("group:everyone") is None


async def test_outbox_returns_and_acks(tmp_path: Path) -> None:
    app, _ = _make(tmp_path)
    adapter = CliAdapter(data_dir=tmp_path)
    await adapter.send("alex", "waiting for you", [])

    async with _client(app) as client:
        first = await client.get(
            "/v1/cli/outbox", params={"person": "alex"}, headers=_bearer(LAPTOP_TOKEN)
        )
        assert [m["text"] for m in first.json()["messages"]] == ["waiting for you"]

        acked = await client.get(
            "/v1/cli/outbox",
            params={"person": "alex", "ack": "true"},
            headers=_bearer(LAPTOP_TOKEN),
        )
        assert len(acked.json()["messages"]) == 1

        after = await client.get(
            "/v1/cli/outbox", params={"person": "alex"}, headers=_bearer(LAPTOP_TOKEN)
        )
        assert after.json()["messages"] == []

    read = outbox_path(tmp_path, "alex").with_suffix(".jsonl.read")
    assert "waiting for you" in read.read_text()
    assert not outbox_path(tmp_path, "alex").with_suffix(".jsonl.acking").exists()


async def test_outbox_ack_keeps_a_line_appended_meanwhile(tmp_path: Path) -> None:
    """A message that arrives during the ack waits for the next call."""
    app, _ = _make(tmp_path)
    adapter = CliAdapter(data_dir=tmp_path)
    await adapter.send("alex", "first", [])
    async with _client(app) as client:
        acked = await client.get(
            "/v1/cli/outbox",
            params={"person": "alex", "ack": "true"},
            headers=_bearer(LAPTOP_TOKEN),
        )
        assert [m["text"] for m in acked.json()["messages"]] == ["first"]
        await adapter.send("alex", "second", [])
        again = await client.get(
            "/v1/cli/outbox",
            params={"person": "alex", "ack": "true"},
            headers=_bearer(LAPTOP_TOKEN),
        )
        assert [m["text"] for m in again.json()["messages"]] == ["second"]


async def test_readyz_reports_the_terminal_channel(tmp_path: Path) -> None:
    """`/readyz` asks every adapter for its health, so the adapter must answer."""
    app, _ = _make(tmp_path)
    async with _client(app) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["checks"]["cli"]["ok"] is True


async def test_outbox_refuses_an_unknown_person(tmp_path: Path) -> None:
    app, _ = _make(tmp_path)
    async with _client(app) as client:
        response = await client.get(
            "/v1/cli/outbox", params={"person": "nobody"}, headers=_bearer(LAPTOP_TOKEN)
        )
    assert response.status_code == 403


# --- refusals, for the "add whoever just messaged you" flow -----------------


async def test_core_can_read_the_refused_handles(tmp_path: Path) -> None:
    """A refused message never reaches core, so the agent cannot name the
    sender. This route is how a member enrolls them without a terminal."""
    app, _ = _make(tmp_path)
    async with _client(app) as client:
        await client.post(
            "/v1/cli/turns/stream",
            json={"person": "stranger", "text": "let me in"},
            headers=_bearer(LAPTOP_TOKEN),
        )
        response = await client.get(
            "/v1/channels/refusals", headers={"Authorization": f"Bearer {CORE_TOKEN}"}
        )
    assert response.status_code == 200
    rows = response.json()["refusals"]
    assert rows[0]["address"] == "stranger"
    assert rows[0]["reason"] == "unknown_sender"
    assert "text" not in rows[0]


async def test_the_refusals_route_is_core_only(tmp_path: Path) -> None:
    app, _ = _make(tmp_path)
    async with _client(app) as client:
        denied = await client.get("/v1/channels/refusals", headers=_bearer(LAPTOP_TOKEN))
        missing = await client.get("/v1/channels/refusals")
    assert denied.status_code == 403
    assert missing.status_code == 401
