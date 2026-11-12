"""Offline tests for the chat routes of the viewer.

Channels is a ``httpx.MockTransport``. The tests cover a turn (JSON and
multipart), the limits, the refusals, the keepalive, the history mapping,
the unconfigured case, and the ``Origin`` check of the turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import textwrap

import httpx
import pytest
from joshua_gateway import viewer, viewer_channels
from joshua_gateway.viewer_api import chat
from joshua_shared import config
from starlette.testclient import TestClient

ALEX = ("alex", "alex-secret")

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
    """)

FRAMES = 'event: delta\ndata: {"text": "Hel"}\n\nevent: done\ndata: {"text": "Hello"}\n\n'
TURNS = "/api/v1/chat/alex/turns"
HISTORY = "/api/v1/chat/alex/history"


class Channels:
    """The fake channels: records the requests, answers what the test sets."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.turn: httpx.Response | None = None
        self.transcript: httpx.Response = httpx.Response(200, json={"messages": []})
        self.unreachable = False
        self.slow_s = 0.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("no route")
        self.requests.append(request)
        self.bodies.append(request.read())
        if request.url.path == viewer_channels.TRANSCRIPT_PATH:
            return self.transcript
        if self.slow_s:
            return httpx.Response(200, stream=_Slow(self.slow_s))
        return self.turn or httpx.Response(200, text=FRAMES)


class _Slow(httpx.AsyncByteStream):
    def __init__(self, delay: float) -> None:
        self.delay = delay

    async def __aiter__(self):
        await asyncio.sleep(self.delay)
        yield FRAMES.encode()


@pytest.fixture
def channels() -> Channels:
    return Channels()


@pytest.fixture
def client(monkeypatch, tmp_path, channels) -> TestClient:
    data = tmp_path / "data"
    (data / "wiki").mkdir(parents=True)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
    monkeypatch.setenv("CHANNELS_URL", "http://channels.test")
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", "viewer-token")
    monkeypatch.setenv("SSE_KEEPALIVE_S", "0.05")

    real = viewer_channels.from_env

    def from_env(env=None):
        base = real(os.environ if env is None else env)
        if base is None:
            return None
        return viewer_channels.ChannelsClient(
            "http://channels.test",
            "viewer-token",
            base.settings,
            transport=httpx.MockTransport(channels.handler),
        )

    monkeypatch.setattr(viewer_channels, "from_env", from_env)
    return TestClient(viewer.build_app())


def test_a_json_turn_streams_its_frames(client, channels) -> None:
    response = client.post(TURNS, json={"text": " hello "}, auth=ALEX)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert 'event: delta\ndata: {"text": "Hel"}\n\n' in response.text
    assert response.text.rstrip().endswith('event: done\ndata: {"text": "Hello"}')
    request = channels.requests[0]
    assert request.headers["authorization"] == "Bearer viewer-token"
    assert json.loads(channels.bodies[0]) == {"person": "alex", "text": "hello"}


def test_a_multipart_turn_reaches_channels_as_multipart(client, channels) -> None:
    response = client.post(
        TURNS,
        data={"text": "look"},
        files={"file": ("scan.pdf", b"%PDF-1.4 data", "application/pdf")},
        auth=ALEX,
    )
    assert response.status_code == 200
    assert channels.requests[0].headers["content-type"].startswith("multipart/form-data")
    body = channels.bodies[0]
    assert b"%PDF-1.4 data" in body and b'filename="scan.pdf"' in body and b"look" in body


def test_a_body_over_the_limit_is_413_and_names_it(client, channels, monkeypatch) -> None:
    monkeypatch.setenv("TURN_MAX_BYTES", "1024")
    response = client.post(TURNS, json={"text": "x" * 4000}, auth=ALEX)
    assert response.status_code == 413
    assert "the limit is 1 KB" in response.json()["error"]
    big = client.post(
        TURNS, data={"text": "a"}, files={"file": ("a.bin", b"z" * 5000, "x/y")}, auth=ALEX
    )
    assert big.status_code == 413
    assert channels.requests == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"text": ""}, "text:"),
        ({}, "text:"),
        ({"text": "x" * 8001}, "text:"),
        ([1], "JSON or multipart"),
    ],
)
def test_a_bad_json_turn_is_422(client, body, message) -> None:
    response = client.post(TURNS, json=body, auth=ALEX)
    assert response.status_code == 422
    assert message in response.json()["error"]


def test_a_body_that_is_not_json_is_422(client) -> None:
    response = client.post(TURNS, content=b"{nope", auth=ALEX)
    assert response.status_code == 422


def test_a_form_with_two_files_or_an_empty_one_is_422(client) -> None:
    two = client.post(
        TURNS, files=[("file", ("a", b"1", "x/y")), ("file", ("b", b"2", "x/y"))], auth=ALEX
    )
    assert two.status_code == 422 and "one file" in two.json()["error"]
    empty = client.post(TURNS, files={"file": ("a", b"", "x/y")}, auth=ALEX)
    assert empty.status_code == 422 and "empty" in empty.json()["error"]
    no_text = client.post(TURNS, data={"text": " "}, files={"x": ("a", b"1", "x/y")}, auth=ALEX)
    assert no_text.status_code == 422


def test_an_unknown_person_is_404(client) -> None:
    for path in ("/api/v1/chat/zed/turns", "/api/v1/chat/Bad_Id/turns"):
        response = client.post(path, json={"text": "hi"}, auth=ALEX)
        assert response.status_code == 404
        assert response.json() == {"error": "Joshua does not know this person"}
    assert client.get("/api/v1/chat/zed/history", auth=ALEX).status_code == 404


@pytest.mark.parametrize(
    ("answer", "status", "message"),
    [
        (httpx.Response(403, json={"reason": "unknown_person"}), 404, "does not know"),
        (httpx.Response(429, json={"reason": "rate_limited"}), 429, "too many"),
        (httpx.Response(503, json={"reason": "core_unavailable"}), 503, "not available"),
        (httpx.Response(503, json={"reason": "pipeline_unavailable"}), 503, "without it"),
        (httpx.Response(401, json={"reason": "unauthorized"}), 502, "not configured"),
        (httpx.Response(503, text="x"), 503, "not available"),
        (httpx.Response(500, text="x"), 502, "refused"),
    ],
)
def test_a_channels_refusal_maps_to_a_status(client, channels, answer, status, message) -> None:
    channels.turn = answer
    response = client.post(TURNS, json={"text": "hi"}, auth=ALEX)
    assert response.status_code == status
    assert message in response.json()["error"]
    assert "reason" in response.json()
    assert "viewer-token" not in response.text


def test_channels_unreachable_is_503(client, channels) -> None:
    channels.unreachable = True
    response = client.post(TURNS, json={"text": "hi"}, auth=ALEX)
    assert response.status_code == 503
    assert response.json() == {"error": "Joshua is unreachable"}
    assert client.get(HISTORY, auth=ALEX).status_code == 503


def test_the_keepalive_goes_out_while_channels_is_silent(client, channels) -> None:
    channels.slow_s = 0.3
    response = client.post(TURNS, json={"text": "hi"}, auth=ALEX)
    assert ": keepalive\n\n" in response.text
    assert "event: done" in response.text


def test_a_failure_after_the_stream_started_is_one_error_frame(client, channels) -> None:
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: delta\ndata: {"text": "a"}\n\n'
            raise httpx.ReadError("cut")

    channels.turn = httpx.Response(200, stream=Broken())
    response = client.post(TURNS, json={"text": "hi"}, auth=ALEX)
    assert response.status_code == 200
    assert 'event: error\ndata: {"message": "the connection to Joshua was lost"}' in response.text


def test_an_error_frame_from_channels_passes_through(client, channels) -> None:
    channels.turn = httpx.Response(200, text='event: error\ndata: {"message": "no"}\n\n')
    response = client.post(TURNS, json={"text": "hi"}, auth=ALEX)
    assert 'event: error\ndata: {"message": "no"}' in response.text


def test_a_cross_site_turn_is_403_and_never_reaches_channels(client, channels) -> None:
    response = client.post(
        TURNS, json={"text": "hi"}, auth=ALEX, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    body = response.json()
    assert set(body) == {"error", "reason"}
    assert channels.requests == []


def test_a_turn_from_the_same_origin_passes(client) -> None:
    response = client.post(
        TURNS, json={"text": "hi"}, auth=ALEX, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 200


def test_the_routes_need_the_sign_in(client, monkeypatch) -> None:
    anonymous = client.post(TURNS, json={"text": "hi"}, auth=("alex", "wrong"))
    assert anonymous.status_code == 401
    assert client.get(HISTORY, auth=("alex", "wrong")).status_code == 401


# -- history ---------------------------------------------------------------------


def test_history_maps_the_rows(client, channels) -> None:
    channels.transcript = httpx.Response(
        200,
        json={
            "conversation_id": "c1",
            "next_before": 7,
            "messages": [
                {
                    "id": 7,
                    "direction": "in",
                    "text": "[speak-brief] hi",
                    "at": "t1",
                    "status": "ok",
                },
                {
                    "id": 8,
                    "direction": "out",
                    "text": "[speak-brief] kept",
                    "at": "t2",
                    "status": "failed",
                    "meta": {"turn_id": "t-1", "tools": ["search", 3]},
                },
                {"id": 9, "direction": "task", "text": "run", "at": "t3"},
                {"id": 10, "direction": "in", "text": "plain", "at": "t4"},
                {"direction": "in", "text": "no id"},
                "junk",
                {"id": 11, "direction": "in", "text": 5},
            ],
        },
    )
    body = client.get(HISTORY, auth=ALEX).json()
    assert body["nextBefore"] == 7
    rows = {m["id"]: m for m in body["messages"]}
    assert set(rows) == {7, 8, 9, 10, 11}
    assert rows[7] == {
        "id": 7,
        "role": "person",
        "text": "hi",
        "status": "ok",
        "at": "t1",
        "spoken": True,
    }
    assert rows[8]["role"] == "joshua" and rows[8]["text"] == "[speak-brief] kept"
    assert rows[8]["status"] == "failed" and rows[8]["turnId"] == "t-1"
    assert rows[8]["tools"] == ["search"] and "spoken" not in rows[8]
    assert rows[9]["role"] == "system"
    assert "spoken" not in rows[10]
    assert rows[11]["text"] == ""


def test_history_asks_for_the_default_limit_and_passes_the_cursor(client, channels) -> None:
    client.get(HISTORY, auth=ALEX)
    assert dict(channels.requests[0].url.params) == {"person": "alex", "limit": "50"}
    client.get(HISTORY + "?limit=200&before=12", auth=ALEX)
    assert dict(channels.requests[1].url.params) == {
        "person": "alex",
        "limit": "200",
        "before": "12",
    }
    channels.transcript = httpx.Response(200, json={"messages": "x", "next_before": "y"})
    assert client.get(HISTORY + "?limit=&before=", auth=ALEX).json() == {
        "messages": [],
        "nextBefore": None,
    }


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=201", "limit=abc", "before=0", "before=-3", "before=x"],
)
def test_history_bounds_are_422(client, channels, query) -> None:
    response = client.get(f"{HISTORY}?{query}", auth=ALEX)
    assert response.status_code == 422
    assert query.split("=")[0] in response.json()["error"]
    assert channels.requests == []


def test_history_refusal_maps(client, channels) -> None:
    channels.transcript = httpx.Response(403, json={"reason": "unknown_person"})
    assert client.get(HISTORY, auth=ALEX).status_code == 404


# -- unconfigured ----------------------------------------------------------------


@pytest.mark.parametrize("missing", ["CHANNELS_URL", "JOSHUA_TOKEN_VIEWER"])
def test_unconfigured_both_routes_are_503_and_readyz_says_so(client, monkeypatch, missing) -> None:
    assert client.get("/readyz").json()["chat"] is True
    monkeypatch.delenv(missing)
    for response in (
        client.post(TURNS, json={"text": "hi"}, auth=ALEX),
        client.get(HISTORY, auth=ALEX),
    ):
        assert response.status_code == 503
        assert response.json() == {"error": "chat is not configured on the Joshua side"}
    assert client.get("/readyz").json()["chat"] is False


def test_unmark_is_exact() -> None:
    assert chat.unmark("[speak-brief]hi") == ("[speak-brief]hi", False)
    assert chat.unmark("[speak-brief]\r\nhi") == ("hi", True)
    assert chat.unmark("[speak-brief] ") == ("", True)
