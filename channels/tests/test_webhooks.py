"""Webhooks events route tests: auth, validation, image download, verbatim.

The image tests run against a small fake HTTP server on localhost, so the real
download path (streaming, size cap, content-type sniff) is exercised with no
external network.
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from adapter_fakes import FakeAdapter
from joshua_channels.app import build_app
from joshua_channels.core_client import TurnAck
from joshua_channels.deliver import ChannelsContext
from joshua_channels.registry import AdapterRegistry
from joshua_channels.webhooks import DownloadOutcome, download_image_urls
from joshua_shared import config as config_module
from joshua_shared.contracts import Attachment

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

SMALL_PNG = b"\x89PNG\r\n\x1a\nsmall-image-bytes"

# What the fake server declares for the oversize image. Bigger than the default
# 25 MB attachment cap, so the download is skipped on the declared size alone.
HUGE_DECLARED_BYTES = 30_000_000


class _ImageHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # silence the default stderr log
        pass

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler naming
        if self.path == "/small.png":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(SMALL_PNG)))
            self.end_headers()
            self.wfile.write(SMALL_PNG)
        elif self.path == "/huge.png":
            # Declare a 30 MB body but send none; the caller skips on the
            # declared size before it reads anything.
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(HUGE_DECLARED_BYTES))
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


@pytest.fixture
def image_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ImageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture(autouse=True)
def _fleet_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE_TOKEN)
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", CHANNELS_TOKEN)
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP_TOKEN)


class FakeCore:
    """Record submitted turns and return a fixed acknowledgement."""

    def __init__(self, ack: TurnAck | None = None) -> None:
        self.ack = ack or TurnAck(accepted=True, status=202, turn_id="t-1")
        self.events: list[Any] = []

    async def submit_turn(self, event: Any) -> TurnAck:
        self.events.append(event)
        return self.ack


class FakePipeline:
    """Return one ``Attachment`` per file the caller stored in the inbox."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str | None, list[str]]] = []

    async def process(
        self, inbox_dir: Path, *, person_id: str | None, group_id: str | None
    ) -> list[Attachment]:
        names = sorted(os.listdir(inbox_dir))
        self.calls.append((person_id, group_id, names))
        return [
            Attachment(path=f"attachments/2026/08/{name}", mime="image/png", name=name)
            for name in names
        ]


def _settings():
    return config_module.parse(CONFIG, env={}, source="<test>")


def _make(
    tmp_path: Path,
    *,
    core: FakeCore | None = None,
    pipeline: FakePipeline | None = None,
    fail_send: bool = False,
) -> tuple[Any, FakeCore, FakeAdapter, FakeAdapter]:
    core = core or FakeCore()
    telegram = FakeAdapter("telegram", {"dm:alex": "998877"}, fail=fail_send)
    imessage = FakeAdapter("imessage", {"group:everyone": GROUP_CHAT}, fail=fail_send)
    registry = AdapterRegistry()
    registry.register(telegram)
    registry.register(imessage)
    context = ChannelsContext(
        settings=_settings(),
        registry=registry,
        data_dir=str(tmp_path),
        core_client=core,
        pipeline=pipeline,
    )
    return build_app(context), core, telegram, imessage


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://channels")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- auth ------------------------------------------------------------------


async def test_laptop_caller_is_accepted(tmp_path: Path) -> None:
    app, core, _, _ = _make(tmp_path)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/events",
            json={"destination": "everyone", "text": "hi"},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert resp.status_code == 202
    assert resp.json() == {"accepted": True}
    assert len(core.events) == 1
    assert core.events[0].handle.type == "webhook"
    assert core.events[0].handle.id == "laptop"


async def test_core_caller_is_forbidden(tmp_path: Path) -> None:
    app, core, _, _ = _make(tmp_path)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/events",
            json={"destination": "everyone", "text": "hi"},
            headers=_bearer(CORE_TOKEN),
        )
    assert resp.status_code == 403
    assert core.events == []


async def test_missing_bearer_is_401(tmp_path: Path) -> None:
    app, _, _, _ = _make(tmp_path)
    async with _client(app) as client:
        resp = await client.post("/v1/events", json={"destination": "everyone", "text": "hi"})
    assert resp.status_code == 401


# --- validation ------------------------------------------------------------


async def test_missing_destination_and_event_type_is_400(tmp_path: Path) -> None:
    app, _, _, _ = _make(tmp_path)
    async with _client(app) as client:
        resp = await client.post("/v1/events", json={"text": "hi"}, headers=_bearer(LAPTOP_TOKEN))
    assert resp.status_code == 400
    assert resp.json()["reason"] == "invalid_event"


async def test_unknown_destination_is_404(tmp_path: Path) -> None:
    app, _, _, _ = _make(tmp_path)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/events",
            json={"destination": "telegram:dm:ghost", "text": "hi"},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert resp.status_code == 404
    assert resp.json()["reason"] == "unknown_channel"


async def test_file_url_is_rejected(tmp_path: Path) -> None:
    app, core, _, _ = _make(tmp_path)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/events",
            json={"destination": "everyone", "text": "hi", "image_urls": ["file:///etc/passwd"]},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert resp.status_code == 400
    assert resp.json()["reason"] == "invalid_image_url"
    assert core.events == []


# --- named events ----------------------------------------------------------


async def test_named_event_forwards_payload(tmp_path: Path) -> None:
    app, core, _, _ = _make(tmp_path)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/events",
            json={"event_type": "morning_announcement", "payload": {"temp": 72}},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert resp.status_code == 202
    event = core.events[0]
    assert event.kind == "event"
    assert event.event_type == "morning_announcement"
    assert event.payload == {"temp": 72}
    assert event.channel == ""


async def test_ignored_event_returns_200(tmp_path: Path) -> None:
    core = FakeCore(TurnAck(accepted=False, status=200))
    app, core, _, _ = _make(tmp_path, core=core)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/events",
            json={"event_type": "unknown_type"},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert resp.status_code == 200
    assert resp.json() == {"ignored": True}


# --- images ----------------------------------------------------------------


async def test_image_url_becomes_attachment(tmp_path: Path, image_server: str) -> None:
    pipeline = FakePipeline()
    app, core, _, _ = _make(tmp_path, pipeline=pipeline)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/events",
            json={
                "destination": "telegram:dm:alex",
                "text": "look",
                "image_urls": [f"{image_server}/small.png"],
            },
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert resp.status_code == 202
    assert pipeline.calls == [("alex", None, ["image-0.png"])]
    event = core.events[0]
    assert event.channel == "telegram:998877"
    assert [a.name for a in event.attachments] == ["image-0.png"]


async def test_oversize_image_is_skipped_with_note(tmp_path: Path, image_server: str) -> None:
    pipeline = FakePipeline()
    app, core, _, _ = _make(tmp_path, pipeline=pipeline)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/events",
            json={
                "destination": "telegram:dm:alex",
                "text": "look",
                "image_urls": [f"{image_server}/huge.png"],
            },
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert resp.status_code == 202
    # The pipeline is never called because nothing was stored.
    assert pipeline.calls == []
    event = core.events[0]
    assert event.attachments == []
    assert "could not be attached" in event.text


# --- verbatim --------------------------------------------------------------


async def test_verbatim_delivers_then_records(tmp_path: Path) -> None:
    app, core, _, imessage = _make(tmp_path)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/events",
            json={"destination": "everyone", "text": "dinner is ready", "verbatim": True},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert resp.status_code == 202
    assert resp.json() == {"accepted": True, "verbatim": True}
    # Delivered straight to the adapter.
    assert imessage.sent == [(GROUP_CHAT, "dinner is ready", [])]
    # And still recorded through core with the verbatim option set.
    assert len(core.events) == 1
    assert core.events[0].options.verbatim is True


async def test_verbatim_send_failure_is_502(tmp_path: Path) -> None:
    app, core, _, _ = _make(tmp_path, fail_send=True)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/events",
            json={"destination": "everyone", "text": "x", "verbatim": True},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert resp.status_code == 502
    # Delivery failed, so nothing is recorded.
    assert core.events == []


async def test_verbatim_without_destination_is_400(tmp_path: Path) -> None:
    app, _, _, _ = _make(tmp_path)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/events",
            json={"event_type": "morning", "verbatim": True},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert resp.status_code == 400
    assert resp.json()["reason"] == "verbatim_needs_destination"


# --- compatibility shim ----------------------------------------------------


async def test_old_inject_fields_are_accepted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app, core, _, imessage = _make(tmp_path)
    with caplog.at_level("WARNING"):
        async with _client(app) as client:
            resp = await client.post(
                "/v1/events",
                json={"channel": "everyone", "content": "hi"},
                headers=_bearer(LAPTOP_TOKEN),
            )
    assert resp.status_code == 202
    event = core.events[0]
    assert event.channel == f"imessage:{GROUP_CHAT}"
    assert event.text == "hi"
    assert any("deprecated" in str(record.msg) for record in caplog.records)


# --- download helper -------------------------------------------------------


async def test_download_stores_within_cap(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"abc")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        outcome = await download_image_urls(client, ["http://img/x"], tmp_path, max_bytes=1000)
    assert outcome.stored == ["image-0.png"]
    assert (tmp_path / "image-0.png").read_bytes() == b"abc"


async def test_download_caps_streamed_body(tmp_path: Path) -> None:
    async def body():
        yield b"x" * 50

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, content=body())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        outcome = await download_image_urls(client, ["http://img/x"], tmp_path, max_bytes=10)
    assert outcome == DownloadOutcome(stored=[], skipped=["http://img/x: too large"])


async def test_download_skips_non_200(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        outcome = await download_image_urls(client, ["http://img/x"], tmp_path, max_bytes=1000)
    assert outcome.stored == []
    assert outcome.skipped == ["http://img/x: HTTP 404"]
