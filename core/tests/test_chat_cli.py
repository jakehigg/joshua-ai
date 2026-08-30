"""The terminal client: SSE parsing, one-shot mode, and error reporting.

The client is synchronous, because the session reads from a terminal. These
tests run it against a small HTTP server on localhost, so the real request path
is exercised with no container and no external network.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from joshua_core import chat

TOKEN = "laptop-tok"
SEEN: list[dict[str, Any]] = []
STATUS = [200]
REFUSAL = ['{"reason": "unknown_sender"}']
OUTBOX: list[dict[str, Any]] = []
OUTBOX_CALLS: list[dict[str, str]] = []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # silence the default stderr log
        pass

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler naming
        from urllib.parse import parse_qs, urlparse

        query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        OUTBOX_CALLS.append(query)
        body = json.dumps({"messages": list(OUTBOX)}).encode()
        if query.get("ack") == "true":
            OUTBOX.clear()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler naming
        length = int(self.headers.get("Content-Length") or 0)
        SEEN.append(json.loads(self.rfile.read(length) or b"{}"))
        if STATUS[0] != 200:
            body = REFUSAL[0].encode()
            self.send_response(STATUS[0])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for frame in (
            b'event: delta\ndata: {"text": "hel"}\n\n',
            b'event: delta\ndata: {"text": "lo"}\n\n',
            b'event: done\ndata: {"turn_id": "t1"}\n\n',
        ):
            self.wfile.write(frame)
            self.wfile.flush()


@pytest.fixture
def channels():
    SEEN.clear()
    STATUS[0] = 200
    REFUSAL[0] = '{"reason": "unknown_sender"}'
    OUTBOX.clear()
    OUTBOX_CALLS.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()


def test_frames_reads_an_event_stream() -> None:
    lines = ["event: delta", 'data: {"text": "hi"}', "", "event: done", "data: {}", ""]
    assert list(chat.frames(lines)) == [("delta", {"text": "hi"}), ("done", {})]


def test_parse_data_falls_back_to_text() -> None:
    assert chat.parse_data(["not json"]) == {"text": "not json"}
    assert chat.parse_data([]) == {}


def test_send_prints_the_reply(channels: str, capsys: pytest.CaptureFixture[str]) -> None:
    with httpx.Client(timeout=None) as client:
        assert chat.send(client, channels, TOKEN, "alex", "hello") is True
    assert capsys.readouterr().out == "hello\n"
    assert SEEN == [{"person": "alex", "text": "hello"}]


def test_send_reports_a_refusal(channels: str, capsys: pytest.CaptureFixture[str]) -> None:
    STATUS[0] = 403
    with httpx.Client(timeout=None) as client:
        assert chat.send(client, channels, TOKEN, "nobody", "hello") is False
    assert "does not know" in capsys.readouterr().err


def test_send_names_another_refusal(channels: str, capsys: pytest.CaptureFixture[str]) -> None:
    STATUS[0] = 403
    REFUSAL[0] = '{"reason": "rate_limited"}'
    with httpx.Client(timeout=None) as client:
        assert chat.send(client, channels, TOKEN, "alex", "hello") is False
    err = capsys.readouterr().err
    assert "rate_limited" in err
    assert "does not know" not in err


def test_main_sends_one_message(channels: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(chat.TOKEN_ENV, TOKEN)
    assert chat.main(["--as", "alex", "--url", channels, "hello", "there"]) == 0
    assert SEEN == [{"person": "alex", "text": "hello there"}]
    # The one-shot drains the outbox first, with ack, so nothing is shown twice.
    assert OUTBOX_CALLS == [{"person": "alex", "ack": "true"}]


def test_main_prints_the_outbox_before_the_reply(
    channels: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(chat.TOKEN_ENV, TOKEN)
    OUTBOX.append({"ts": "2026-08-27T07:30:00+00:00", "text": "reminder: bins", "attachments": []})
    assert chat.main(["--as", "alex", "--url", channels, "hello"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[joshua earlier, 2026-08-27 07:30] reminder: bins\n")
    assert out.endswith("hello\n")
    assert OUTBOX == []


def test_drain_outbox_ignores_a_failed_request(capsys: pytest.CaptureFixture[str]) -> None:
    with httpx.Client(timeout=1) as client:
        assert chat.drain_outbox(client, "http://127.0.0.1:9", TOKEN, "alex") == 0
    assert capsys.readouterr().out == ""


def test_main_needs_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(chat.TOKEN_ENV, raising=False)
    assert chat.main(["--as", "alex", "hello"]) == 2
