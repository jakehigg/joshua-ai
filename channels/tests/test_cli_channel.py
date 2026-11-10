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
from joshua_channels.core_client import CoreClient
from joshua_channels.deliver import ChannelsContext
from joshua_channels.guard import Guard
from joshua_channels.registry import AdapterRegistry
from joshua_shared import config as config_module
from joshua_shared.http import FleetClient

LAPTOP_TOKEN = "laptop-tok"
CORE_TOKEN = "core-tok"
CHANNELS_TOKEN = "chan-tok"

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


def test_the_router_serves_every_cli_route() -> None:
    paths = {r.path for r in build_cli_router().routes}
    assert paths == {"/v1/cli/turns/stream", "/v1/cli/outbox", "/v1/cli/transcript"}


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


# --- transcript ------------------------------------------------------------

TRANSCRIPT_BODY = {
    "conversation_id": "conv-1",
    "messages": [
        {
            "id": 7,
            "direction": "in",
            "text": "the secret plan, in full",
            "status": "ok",
            "at": "2026-03-04T12:00:00+00:00",
            "meta": {"turn_id": "t1"},
        },
        {
            "id": 8,
            "direction": "out",
            "text": "Joshua's whole answer",
            "status": "ok",
            "at": "2026-03-04T12:00:02+00:00",
            "meta": {"turn_id": "t1", "tools": []},
        },
    ],
    "next_before": 7,
}


class TranscriptCore:
    """A real ``CoreClient`` over ``httpx.MockTransport``. It records each
    request and answers with ``status`` and ``body``, or raises ``error``."""

    def __init__(self, status: int = 200, body: Any = None, error: Exception | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._status = status
        self._body = TRANSCRIPT_BODY if body is None else body
        self._error = error
        fleet = FleetClient(
            "http://core", CHANNELS_TOKEN, transport=httpx.MockTransport(self._handle)
        )
        self.client = CoreClient(fleet)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return httpx.Response(self._status, json=self._body)


def _make_transcript(tmp_path: Path, core: TranscriptCore) -> Any:
    app, _ = _make(tmp_path, core=core.client)
    return app


async def _read(app: Any, params: dict[str, str] | None, token: str | None = LAPTOP_TOKEN) -> Any:
    headers = _bearer(token) if token else {}
    async with _client(app) as client:
        return await client.get("/v1/cli/transcript", params=params or {}, headers=headers)


async def test_transcript_passes_core_body_through(tmp_path: Path) -> None:
    core = TranscriptCore()
    response = await _read(_make_transcript(tmp_path, core), {"person": "alex"})
    assert response.status_code == 200
    assert response.json() == TRANSCRIPT_BODY


async def test_transcript_asks_core_with_the_channels_token(tmp_path: Path) -> None:
    core = TranscriptCore()
    await _read(_make_transcript(tmp_path, core), {"person": "alex", "limit": "20", "before": "99"})
    request = core.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/v1/transcript"
    assert request.headers["authorization"] == f"Bearer {CHANNELS_TOKEN}"
    assert dict(request.url.params) == {
        "channel": "cli:alex",
        "person": "alex",
        "limit": "20",
        "before": "99",
    }


async def test_transcript_sends_no_limit_or_before_it_was_not_given(tmp_path: Path) -> None:
    core = TranscriptCore()
    await _read(_make_transcript(tmp_path, core), {"person": "gwen"})
    assert dict(core.requests[0].url.params) == {"channel": "cli:gwen", "person": "gwen"}


async def test_transcript_passes_core_status_through(tmp_path: Path) -> None:
    core = TranscriptCore(status=422, body={"detail": "bad limit"})
    response = await _read(_make_transcript(tmp_path, core), {"person": "alex", "limit": "0"})
    assert response.status_code == 422
    assert response.json() == {"detail": "bad limit"}


async def test_transcript_refuses_an_unknown_person(tmp_path: Path) -> None:
    core = TranscriptCore()
    response = await _read(_make_transcript(tmp_path, core), {"person": "nobody"})
    assert response.status_code == 403
    assert response.json() == {"reason": "unknown_person"}
    assert core.requests == []


async def test_transcript_needs_a_person(tmp_path: Path) -> None:
    core = TranscriptCore()
    response = await _read(_make_transcript(tmp_path, core), None)
    assert response.status_code == 400
    assert response.json() == {"reason": "bad_request"}
    assert core.requests == []


async def test_transcript_needs_a_bearer(tmp_path: Path) -> None:
    core = TranscriptCore()
    response = await _read(_make_transcript(tmp_path, core), {"person": "alex"}, token=None)
    assert response.status_code == 401
    assert core.requests == []


async def test_transcript_refuses_a_caller_not_on_the_list(tmp_path: Path) -> None:
    core = TranscriptCore()
    response = await _read(_make_transcript(tmp_path, core), {"person": "alex"}, token=CORE_TOKEN)
    assert response.status_code == 403
    assert response.json() == {"reason": "forbidden"}
    assert core.requests == []


async def test_transcript_with_core_down_is_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("joshua_shared.http.BACKOFF_BASE_S", 0.0)
    core = TranscriptCore(error=httpx.ConnectError("refused"))
    response = await _read(_make_transcript(tmp_path, core), {"person": "alex"})
    assert response.status_code == 503
    assert response.json() == {"reason": "core_unavailable"}


async def test_transcript_with_no_core_client_is_503(tmp_path: Path) -> None:
    settings = config_module.parse(CONFIG, env={}, source="<test>")
    context = ChannelsContext(
        settings=settings,
        registry=AdapterRegistry(),
        data_dir=str(tmp_path),
        guard=Guard(settings, settings.channels.limits, config_provider=lambda: settings),
        core_client=None,
    )
    response = await _read(build_app(context), {"person": "alex"})
    assert response.status_code == 503
    assert response.json() == {"reason": "core_unavailable"}


async def test_transcript_logs_no_message_text(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("joshua_shared.http.BACKOFF_BASE_S", 0.0)
    with caplog.at_level("DEBUG"):
        await _read(_make_transcript(tmp_path, TranscriptCore()), {"person": "alex"})
        down = TranscriptCore(error=httpx.ConnectError("refused"))
        await _read(_make_transcript(tmp_path, down), {"person": "alex"})
    assert caplog.records
    for record in caplog.records:
        line = record.getMessage() + json.dumps(record.msg, default=str)
        assert "secret plan" not in line
        assert "whole answer" not in line
