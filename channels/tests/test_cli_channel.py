"""The terminal channel: guard, streaming, attachments, and the outbox.

The terminal is a channel, so an unknown person is refused at channels and never
reaches core. These tests run the real routes over ``ASGITransport``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from joshua_channels.adapters.cli import CliAdapter, outbox_path
from joshua_channels.app import build_app
from joshua_channels.attachments import AttachmentPipeline
from joshua_channels.cli_routes import _relay, build_cli_router, inbox_name
from joshua_channels.core_client import CoreClient
from joshua_channels.deliver import ChannelsContext
from joshua_channels.guard import Guard
from joshua_channels.registry import AdapterRegistry
from joshua_shared import config as config_module
from joshua_shared.attachments import NO_CAPTION_TEXT
from joshua_shared.contracts import Chat, Handle, TurnEvent
from joshua_shared.http import FleetClient
from pdf_fixtures import pdf_with_text
from PIL import Image

LAPTOP_TOKEN = "laptop-tok"
CORE_TOKEN = "core-tok"
CHANNELS_TOKEN = "chan-tok"

TIMEZONE = "America/New_York"
CLOCK = datetime(2026, 8, 27, 15, 0, tzinfo=UTC)

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


def _make(
    tmp_path: Path, *, core: Any = None, pipeline: Any = None, config: str = CONFIG
) -> tuple[Any, Any]:
    core = core or FakeStreamCore()
    settings = config_module.parse(config, env={}, source="<test>")
    registry = AdapterRegistry()
    registry.register(CliAdapter(data_dir=tmp_path))
    context = ChannelsContext(
        settings=settings,
        registry=registry,
        data_dir=str(tmp_path),
        guard=Guard(settings, settings.channels.limits, config_provider=lambda: settings),
        core_client=core,
        pipeline=pipeline,
    )
    return build_app(context), core


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://channels")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_the_router_serves_every_cli_route() -> None:
    paths = {r.path for r in build_cli_router().routes}
    assert paths == {
        "/v1/cli/turns/stream",
        "/v1/cli/outbox",
        "/v1/cli/transcript",
        "/v1/cli/held",
        "/v1/cli/held/take",
        "/v1/cli/held/{hold_id}",
    }


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


# --- multipart and attachments ---------------------------------------------

# Appended to CONFIG for the test of the attachment cap.
SMALL_LIMIT = """
channels:
  limits:
    max_attachment_bytes: 1000
"""


def _pipeline(tmp_path: Path) -> AttachmentPipeline:
    """The real pipeline on a fixed clock, so the stored month is known."""
    return AttachmentPipeline(data_dir=tmp_path, timezone=TIMEZONE, clock=lambda: CLOCK)


def _month_prefix(person: str) -> str:
    local = CLOCK.astimezone(ZoneInfo(TIMEZONE))
    return f"people/{person}/attachments/{local:%Y/%m}/"


def _jpeg() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (40, 30), (200, 80, 20)).save(buffer, format="JPEG")
    return buffer.getvalue()


async def _post_parts(app: Any, files: list[Any], **fields: str) -> httpx.Response:
    async with _client(app) as client:
        return await client.post(
            "/v1/cli/turns/stream", data=fields, files=files, headers=_bearer(LAPTOP_TOKEN)
        )


def _dict_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [r.msg for r in caplog.records if isinstance(r.msg, dict)]


async def test_a_multipart_turn_carries_every_stored_file(tmp_path: Path) -> None:
    app, core = _make(tmp_path, pipeline=_pipeline(tmp_path))
    response = await _post_parts(
        app,
        [
            ("file", ("photo.jpg", _jpeg(), "image/jpeg")),
            ("file", ("report.pdf", pdf_with_text(["the plan"]), "application/pdf")),
        ],
        person="alex",
        text="look at these",
    )
    assert response.status_code == 200
    event = core.events[0]
    assert event.text == "look at these"
    assert [a.original_name for a in event.attachments] == ["photo.jpg", "report.pdf"]
    assert [a.mime for a in event.attachments] == ["image/jpeg", "application/pdf"]
    for attachment in event.attachments:
        assert attachment.path.startswith(_month_prefix("alex"))
        assert (tmp_path / attachment.path).exists()
    # The pipeline deletes the inbox directory it ingested.
    assert list((tmp_path / "inbox").iterdir()) == []


async def test_a_file_with_no_caption_gets_the_placeholder(tmp_path: Path) -> None:
    app, core = _make(tmp_path, pipeline=_pipeline(tmp_path))
    response = await _post_parts(
        app, [("file", ("photo.jpg", _jpeg(), "image/jpeg"))], person="alex"
    )
    assert response.status_code == 200
    assert core.events[0].text == NO_CAPTION_TEXT
    assert len(core.events[0].attachments) == 1


async def test_a_zip_part_is_skipped_and_the_turn_goes_through(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app, core = _make(tmp_path, pipeline=_pipeline(tmp_path))
    with caplog.at_level(logging.INFO, logger="channels.attachments"):
        response = await _post_parts(
            app,
            [("file", ("payload.zip", b"PK\x03\x04and the rest", "application/zip"))],
            person="alex",
            text="unpack this",
        )
    assert response.status_code == 200
    assert core.events[0].attachments == []
    assert core.events[0].text == "unpack this"
    skipped = [r for r in _dict_records(caplog) if r.get("reason") == "disallowed_type"]
    assert skipped and skipped[0]["mime"] == "application/zip"


async def test_a_part_over_the_cap_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app, core = _make(tmp_path, pipeline=_pipeline(tmp_path), config=CONFIG + SMALL_LIMIT)
    with caplog.at_level(logging.INFO, logger="channels.cli"):
        response = await _post_parts(
            app,
            [("file", ("notes.txt", b"n" * 2000, "text/plain"))],
            person="alex",
            text="my notes",
        )
    assert response.status_code == 200
    assert core.events[0].attachments == []
    assert [r["reason"] for r in _dict_records(caplog) if r.get("reason")] == ["too_large"]


async def test_the_guard_gets_the_real_byte_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _make(tmp_path, pipeline=_pipeline(tmp_path))
    guard = app.state.ctx.guard
    seen: list[int] = []
    check = guard.check

    def record(**kwargs: Any) -> Any:
        seen.append(kwargs["attachment_bytes"])
        return check(**kwargs)

    monkeypatch.setattr(guard, "check", record)
    data = _jpeg()
    response = await _post_parts(
        app, [("file", ("photo.jpg", data, "image/jpeg"))], person="alex", text="this one"
    )
    assert response.status_code == 200
    assert seen == [len(data)]


async def test_a_multipart_turn_with_no_pipeline_is_503(tmp_path: Path) -> None:
    app, core = _make(tmp_path)
    response = await _post_parts(
        app, [("file", ("photo.jpg", _jpeg(), "image/jpeg"))], person="alex", text="hello"
    )
    assert response.status_code == 503
    assert response.json() == {"reason": "pipeline_unavailable"}
    assert core.events == []


async def test_an_unknown_person_with_a_file_stores_nothing(tmp_path: Path) -> None:
    """The guard runs before the pipeline, so a refused turn writes no file."""
    app, core = _make(tmp_path, pipeline=_pipeline(tmp_path))
    response = await _post_parts(
        app, [("file", ("photo.jpg", _jpeg(), "image/jpeg"))], person="nobody", text="hello"
    )
    assert response.status_code == 403
    assert core.events == []
    assert not (tmp_path / "people").exists()


async def test_a_multipart_turn_with_no_person_is_a_bad_request(tmp_path: Path) -> None:
    app, _ = _make(tmp_path, pipeline=_pipeline(tmp_path))
    response = await _post_parts(
        app, [("file", ("photo.jpg", _jpeg(), "image/jpeg"))], text="hello"
    )
    assert response.status_code == 400


async def test_a_part_name_cannot_escape_the_inbox(tmp_path: Path) -> None:
    app, core = _make(tmp_path, pipeline=_pipeline(tmp_path))
    response = await _post_parts(
        app,
        [("file", ("../../escape.txt", b"just some text", "text/plain"))],
        person="alex",
        text="where does this land",
    )
    assert response.status_code == 200
    stored = core.events[0].attachments[0]
    assert stored.path.startswith(_month_prefix("alex"))
    assert not (tmp_path.parent / "escape.txt").exists()


def test_inbox_name_keeps_the_name_and_strips_the_rest() -> None:
    assert inbox_name("../../etc/notes.txt", 0) == "notes.txt"
    assert inbox_name("my photo!.jpg", 1) == "my_photo_.jpg"
    assert inbox_name(None, 2) == "file-2"


async def test_the_json_turn_takes_no_file(tmp_path: Path) -> None:
    """`make chat` and every other `cli` caller post JSON. It is unchanged."""
    app, core = _make(tmp_path, pipeline=_pipeline(tmp_path))
    async with _client(app) as client:
        response = await client.post(
            "/v1/cli/turns/stream",
            json={"person": "alex", "text": "hello"},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert response.status_code == 200
    assert core.events[0].text == "hello"
    assert core.events[0].attachments == []
    assert not (tmp_path / "inbox").exists()
    assert not (tmp_path / "people" / "alex" / "attachments").exists()


async def test_a_json_turn_may_name_a_workspace(tmp_path: Path) -> None:
    app, core = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            "/v1/cli/turns/stream",
            json={"person": "alex", "text": "next step?", "workspace": " solar "},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert response.status_code == 200
    event = core.events[0]
    assert event.channel == "workspace:solar"
    assert event.chat.kind == "dm" and event.chat.id == "alex"
    assert event.handle.type == "cli" and event.handle.id == "alex"
    assert event.thread is None  # a workspace is its own thread


async def test_a_multipart_turn_may_name_a_workspace(tmp_path: Path) -> None:
    app, core = _make(tmp_path, pipeline=_pipeline(tmp_path))
    response = await _post_parts(
        app,
        [("file", ("notes.txt", b"quote: 12000", "text/plain"))],
        person="alex",
        text="file this",
        workspace="solar",
    )
    assert response.status_code == 200
    assert core.events[0].channel == "workspace:solar"
    assert len(core.events[0].attachments) == 1


async def test_a_turn_without_a_workspace_stays_on_the_terminal(tmp_path: Path) -> None:
    app, core = _make(tmp_path)
    async with _client(app) as client:
        await client.post(
            "/v1/cli/turns/stream",
            json={"person": "alex", "text": "hi", "workspace": 7},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert core.events[0].channel == "cli:alex"
    assert core.events[0].thread == "main"  # the terminal and the viewer join the main thread


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
        "thread": "main",
        "limit": "20",
        "before": "99",
    }


async def test_transcript_reads_a_workspace_thread(tmp_path: Path) -> None:
    core = TranscriptCore()
    await _read(_make_transcript(tmp_path, core), {"person": "alex", "workspace": "solar"})
    assert dict(core.requests[0].url.params) == {"channel": "workspace:solar", "person": "alex"}


async def test_transcript_sends_no_limit_or_before_it_was_not_given(tmp_path: Path) -> None:
    core = TranscriptCore()
    await _read(_make_transcript(tmp_path, core), {"person": "gwen"})
    assert dict(core.requests[0].url.params) == {
        "channel": "cli:gwen",
        "person": "gwen",
        "thread": "main",
    }


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


async def test_transcript_passes_pending_and_attachments_through(tmp_path: Path) -> None:
    pending = {
        "turn_id": "t2",
        "state": "queued",
        "since": "2026-03-04T12:01:00+00:00",
        "text": "x",
    }
    files = [{"path": "wiki/attachments/2026/11/a.jpg", "mime": "image/jpeg", "name": "a.jpg"}]
    body = {
        **TRANSCRIPT_BODY,
        "pending": pending,
        "messages": [{**TRANSCRIPT_BODY["messages"][0], "meta": {"attachments": files}}],
    }
    response = await _read(
        _make_transcript(tmp_path, TranscriptCore(body=body)), {"person": "alex"}
    )
    assert response.json()["pending"] == pending
    assert response.json()["messages"][0]["meta"]["attachments"] == files


# --- a caller that leaves ----------------------------------------------------


class SlowCore:
    """A core that waits for a release before the reply, and notes how far it got."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.closed_early = False

    async def stream_turn(self, event: Any):
        try:
            yield "accepted", {"turn_id": "t9"}
            yield "status", {"state": "queued"}
            await self.release.wait()
            yield "done", {"turn_id": "t9", "text": "late", "tools": []}
            self.finished.set()
        except (asyncio.CancelledError, GeneratorExit):
            self.closed_early = True
            raise


async def test_the_frames_of_core_pass_through_in_order(tmp_path: Path) -> None:
    core = SlowCore()
    core.release.set()
    app, _ = _make(tmp_path, core=core)
    async with _client(app) as client:
        response = await client.post(
            "/v1/cli/turns/stream",
            json={"person": "alex", "text": "hi"},
            headers=_bearer(LAPTOP_TOKEN),
        )
    names = [
        line.removeprefix("event: ")
        for line in response.text.splitlines()
        if line.startswith("event:")
    ]
    assert names == ["accepted", "status", "done"]


async def test_a_caller_that_leaves_does_not_close_the_stream_to_core(tmp_path: Path) -> None:
    """ASGITransport buffers a response, so the generator is read directly and
    its reader is cancelled, as the server cancels the response of a caller that left."""
    core = SlowCore()
    _, _ = _make(tmp_path, core=core)
    ctx = SimpleNamespace(core_client=core)
    event = TurnEvent(
        channel="cli:alex",
        chat=Chat(kind="dm", id="alex"),
        handle=Handle(type="cli", id="alex"),
        text="hi",
    )
    seen: list[str] = []

    async def reader() -> None:
        async for frame in _relay(ctx, event):  # type: ignore[arg-type]
            seen.append(frame)

    task = asyncio.create_task(reader())
    for _ in range(100):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The caller is gone. Core is still waiting for the reply.
    assert not core.finished.is_set()
    core.release.set()
    await asyncio.wait_for(core.finished.wait(), 2)
    assert core.closed_early is False
    assert len(seen) == 2


# --- the hold --------------------------------------------------------------

HELD_BODY = {"held": [{"id": "h1", "text": "and one more", "at": "2026-11-14T10:00:00+00:00"}]}


def _make_held(tmp_path: Path, core: TranscriptCore) -> Any:
    app, _ = _make(tmp_path, core=core.client)
    return app


async def test_a_held_message_goes_to_core_with_the_channels_token(tmp_path: Path) -> None:
    core = TranscriptCore(body=HELD_BODY)
    async with _client(_make_held(tmp_path, core)) as client:
        response = await client.post(
            "/v1/cli/held",
            json={"person": "alex", "text": "and one more"},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert response.status_code == 200
    assert response.json() == HELD_BODY
    request = core.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/holds"
    assert request.headers["authorization"] == f"Bearer {CHANNELS_TOKEN}"
    assert json.loads(request.content) == {
        "channel": "cli:alex",
        "person": "alex",
        "text": "and one more",
    }


async def test_a_workspace_message_is_held_for_that_thread(tmp_path: Path) -> None:
    core = TranscriptCore(body=HELD_BODY)
    async with _client(_make_held(tmp_path, core)) as client:
        await client.post(
            "/v1/cli/held",
            json={"person": "alex", "text": "later", "workspace": "solar"},
            headers=_bearer(LAPTOP_TOKEN),
        )
    assert json.loads(core.requests[0].content)["channel"] == "workspace:solar"


async def test_the_hold_is_read_and_taken_and_one_message_removed(tmp_path: Path) -> None:
    core = TranscriptCore(body=HELD_BODY)
    async with _client(_make_held(tmp_path, core)) as client:
        listed = await client.get(
            "/v1/cli/held", params={"person": "alex"}, headers=_bearer(LAPTOP_TOKEN)
        )
        await client.post(
            "/v1/cli/held/take", json={"person": "alex"}, headers=_bearer(LAPTOP_TOKEN)
        )
        await client.delete(
            "/v1/cli/held/h1", params={"person": "alex"}, headers=_bearer(LAPTOP_TOKEN)
        )
    assert listed.json() == HELD_BODY
    read, take, drop = core.requests
    assert read.method == "GET"
    assert dict(read.url.params) == {"channel": "cli:alex", "person": "alex"}
    assert (take.method, take.url.path) == ("POST", "/v1/holds/take")
    assert json.loads(take.content) == {"channel": "cli:alex", "person": "alex"}
    assert (drop.method, drop.url.path) == ("DELETE", "/v1/holds/h1")
    assert dict(drop.url.params) == {"channel": "cli:alex", "person": "alex"}


async def test_an_empty_held_message_never_reaches_core(tmp_path: Path) -> None:
    core = TranscriptCore(body=HELD_BODY)
    async with _client(_make_held(tmp_path, core)) as client:
        response = await client.post(
            "/v1/cli/held", json={"person": "alex", "text": "   "}, headers=_bearer(LAPTOP_TOKEN)
        )
    assert response.status_code == 400
    assert response.json() == {"reason": "bad_request"}
    assert core.requests == []


async def test_the_hold_refuses_an_unknown_person_and_a_caller_not_on_the_list(
    tmp_path: Path,
) -> None:
    core = TranscriptCore(body=HELD_BODY)
    async with _client(_make_held(tmp_path, core)) as client:
        unknown = await client.get(
            "/v1/cli/held", params={"person": "nobody"}, headers=_bearer(LAPTOP_TOKEN)
        )
        forbidden = await client.get(
            "/v1/cli/held", params={"person": "alex"}, headers=_bearer(CORE_TOKEN)
        )
        nobody = await client.get("/v1/cli/held", params={"person": "alex"})
    assert unknown.status_code == 403
    assert unknown.json() == {"reason": "unknown_person"}
    assert forbidden.status_code == 403
    assert nobody.status_code == 401
    assert core.requests == []


async def test_the_hold_passes_a_core_refusal_through(tmp_path: Path) -> None:
    core = TranscriptCore(status=404, body={"reason": "unknown_hold"})
    async with _client(_make_held(tmp_path, core)) as client:
        response = await client.delete(
            "/v1/cli/held/gone", params={"person": "alex"}, headers=_bearer(LAPTOP_TOKEN)
        )
    assert response.status_code == 404
    assert response.json() == {"reason": "unknown_hold"}
