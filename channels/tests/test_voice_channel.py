"""The voice channel: auth, identity, roaming, and the OpenAI shapes.

The route is what a voice front end sees, so these tests speak to it the way a
front end does: an OpenAI body in, OpenAI chunks out. They run the real routes
over ``ASGITransport`` against a fake core.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from joshua_channels.adapters import voice as voice_adapter
from joshua_channels.adapters.voice import IdentityHold
from joshua_channels.app import build_app
from joshua_channels.deliver import ChannelsContext
from joshua_channels.guard import Guard
from joshua_channels.registry import AdapterRegistry
from joshua_channels.voice_routes import UNNAMED_SPEAKER
from joshua_shared import config as config_module

VOICE_TOKEN = "voice-tok"
LAPTOP_TOKEN = "laptop-tok"

COMPLETIONS = "/v1/voice/chat/completions"

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    handles:
      voice: alex
  - id: gwen
    name: Gwen
    role: guest
    handles:
      voice: gwen
channels:
  voice:
    allowed_callers:
      - voice
    thread: person
    min_confidence: "0.6"
    identity_hold_s: "120"
    unknown_sender: drop
"""

NO_VOICE_CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
"""


class FakeStreamCore:
    """A core that streams two deltas and a done frame."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def stream_turn(self, event: Any):
        self.events.append(event)
        yield "delta", {"text": "it is "}
        yield "delta", {"text": "raining"}
        yield "done", {"turn_id": "t1", "text": "it is raining", "tools": []}


class SilentCore:
    """A core that streams nothing but the final text, as a stub backend does."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def stream_turn(self, event: Any):
        self.events.append(event)
        yield "done", {"turn_id": "t1", "text": "one chunk only", "tools": []}


class FailingCore:
    """A core whose turn raises. The message must not reach the speaker."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def stream_turn(self, event: Any):
        self.events.append(event)
        yield "error", {"message": "psycopg.OperationalError: password authentication failed"}


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_VOICE", VOICE_TOKEN)
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP_TOKEN)


def _make(tmp_path: Path, *, core: Any = None, config: str = CONFIG) -> tuple[Any, Any, Any]:
    core = core or FakeStreamCore()
    settings = config_module.parse(config, env={}, source="<test>")
    context = ChannelsContext(
        settings=settings,
        registry=AdapterRegistry(),
        data_dir=str(tmp_path),
        guard=Guard(settings, settings.channels.limits, config_provider=lambda: settings),
        core_client=core,
    )
    app = build_app(context)
    return app, core, context


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://channels")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _body(text: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "joshua",
        "messages": [{"role": "user", "content": text}],
        "user": "voice:office",
        "stream": True,
    }
    body.update(extra)
    return body


def _chunks(text: str) -> list[dict[str, Any]]:
    """Every ``data:`` payload in an SSE body, without the terminator."""
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :]
        if payload == "[DONE]":
            continue
        out.append(json.loads(payload))
    return out


def _content(chunks: list[dict[str, Any]]) -> str:
    return "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)


# --- auth -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_wrong_token_is_401(tmp_path: Path) -> None:
    app, core, _ = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            COMPLETIONS, json=_body("hello"), headers=_bearer("not-a-token")
        )
    assert response.status_code == 401
    assert core.events == []


@pytest.mark.asyncio
async def test_an_identity_that_may_not_speak_is_403(tmp_path: Path) -> None:
    app, core, _ = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            COMPLETIONS, json=_body("hello"), headers=_bearer(LAPTOP_TOKEN)
        )
    assert response.status_code == 403
    assert core.events == []


@pytest.mark.asyncio
async def test_there_is_no_route_without_the_voice_section(tmp_path: Path) -> None:
    app, core, _ = _make(tmp_path, config=NO_VOICE_CONFIG)
    async with _client(app) as client:
        completions = await client.post(
            COMPLETIONS, json=_body("hello"), headers=_bearer(VOICE_TOKEN)
        )
        models = await client.get("/v1/models", headers=_bearer(VOICE_TOKEN))
    assert completions.status_code == 404
    assert models.status_code == 404
    assert core.events == []


@pytest.mark.asyncio
async def test_models_reports_one_model(tmp_path: Path) -> None:
    app, _core, _ = _make(tmp_path)
    async with _client(app) as client:
        response = await client.get("/v1/models", headers=_bearer(VOICE_TOKEN))
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert [item["id"] for item in body["data"]] == [voice_adapter.MODEL_ID]


# --- the reply --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_streamed_turn_is_openai_chunks_and_ends_with_done(tmp_path: Path) -> None:
    app, core, _ = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            COMPLETIONS,
            json=_body("what is the weather", speaker="person:alex"),
            headers=_bearer(VOICE_TOKEN),
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.endswith("data: [DONE]\n\n")

    chunks = _chunks(response.text)
    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert _content(chunks) == "it is raining"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    # One completion id for the whole reply.
    assert len({chunk["id"] for chunk in chunks}) == 1
    assert core.events[0].text == "what is the weather"


@pytest.mark.asyncio
async def test_a_backend_that_does_not_stream_still_answers_once(tmp_path: Path) -> None:
    app, _core, _ = _make(tmp_path, core=SilentCore())
    async with _client(app) as client:
        response = await client.post(
            COMPLETIONS, json=_body("hello", speaker="person:alex"), headers=_bearer(VOICE_TOKEN)
        )
    assert _content(_chunks(response.text)) == "one chunk only"


@pytest.mark.asyncio
async def test_stream_false_returns_one_completion(tmp_path: Path) -> None:
    app, _core, _ = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            COMPLETIONS,
            json=_body("hello", speaker="person:alex", stream=False),
            headers=_bearer(VOICE_TOKEN),
        )
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "it is raining"}
    assert body["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_a_failed_turn_says_one_sentence_and_leaks_nothing(tmp_path: Path) -> None:
    app, _core, _ = _make(tmp_path, core=FailingCore())
    async with _client(app) as client:
        response = await client.post(
            COMPLETIONS, json=_body("hello", speaker="person:alex"), headers=_bearer(VOICE_TOKEN)
        )
    assert _content(_chunks(response.text)) == voice_adapter.TURN_FAILED_REPLY
    assert "password" not in response.text
    assert "OperationalError" not in response.text


@pytest.mark.asyncio
async def test_a_reply_marker_passes_through_untouched(tmp_path: Path) -> None:
    class MarkerCore:
        events: list[Any] = []

        async def stream_turn(self, event: Any):
            self.events.append(event)
            yield "delta", {"text": "goodbye [DONE]"}
            yield "done", {"turn_id": "t1", "text": "goodbye [DONE]", "tools": []}

    app, _core, _ = _make(tmp_path, core=MarkerCore())
    async with _client(app) as client:
        response = await client.post(
            COMPLETIONS, json=_body("bye", speaker="person:alex"), headers=_bearer(VOICE_TOKEN)
        )
    assert _content(_chunks(response.text)) == "goodbye [DONE]"


# --- who is speaking --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_speaker_is_dropped_and_reaches_no_turn(tmp_path: Path) -> None:
    app, core, context = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            COMPLETIONS, json=_body("hello", speaker="person:guest"), headers=_bearer(VOICE_TOKEN)
        )
    assert response.status_code == 200
    assert _content(_chunks(response.text)) == voice_adapter.IGNORE_REPLY
    assert core.events == []
    # The refusal is in the audit ring, so an operator can enroll the speaker.
    recent = context.guard.recent()
    assert recent[-1]["address"] == "guest"
    assert recent[-1]["reason"] == "unknown_sender"


@pytest.mark.asyncio
async def test_unknown_sender_reply_says_one_sentence(tmp_path: Path) -> None:
    config = CONFIG.replace("unknown_sender: drop", "unknown_sender: reply")
    app, core, _ = _make(tmp_path, config=config)
    async with _client(app) as client:
        response = await client.post(
            COMPLETIONS, json=_body("hello", speaker="person:guest"), headers=_bearer(VOICE_TOKEN)
        )
    assert _content(_chunks(response.text)) == voice_adapter.UNKNOWN_SPEAKER_REPLY
    assert core.events == []


@pytest.mark.asyncio
async def test_a_member_roams_between_devices_on_one_conversation(tmp_path: Path) -> None:
    app, core, _ = _make(tmp_path)
    async with _client(app) as client:
        await client.post(
            COMPLETIONS,
            json=_body("first", speaker="person:alex", user="voice:office"),
            headers=_bearer(VOICE_TOKEN),
        )
        await client.post(
            COMPLETIONS,
            json=_body("second", speaker="person:alex", user="voice:kitchen"),
            headers=_bearer(VOICE_TOKEN),
        )
    assert [event.channel for event in core.events] == [
        voice_adapter.ROAMING_CHANNEL,
        voice_adapter.ROAMING_CHANNEL,
    ]
    # The room is not in the channel id, so it rides as framing.
    assert "office" in core.events[0].framing
    assert "kitchen" in core.events[1].framing


@pytest.mark.asyncio
async def test_a_guest_stays_on_the_device(tmp_path: Path) -> None:
    app, core, _ = _make(tmp_path)
    async with _client(app) as client:
        await client.post(
            COMPLETIONS,
            json=_body("hello", speaker="person:gwen", user="voice:office"),
            headers=_bearer(VOICE_TOKEN),
        )
    assert core.events[0].channel == "voice:office"
    assert core.events[0].chat.id == "office"


@pytest.mark.asyncio
async def test_thread_device_keeps_a_member_on_the_device(tmp_path: Path) -> None:
    config = CONFIG.replace("thread: person", "thread: device")
    app, core, _ = _make(tmp_path, config=config)
    async with _client(app) as client:
        await client.post(
            COMPLETIONS,
            json=_body("hello", speaker="person:alex", user="voice:office"),
            headers=_bearer(VOICE_TOKEN),
        )
    assert core.events[0].channel == "voice:office"


@pytest.mark.asyncio
async def test_a_low_score_keeps_the_identity_the_device_holds(tmp_path: Path) -> None:
    app, core, _ = _make(tmp_path)
    async with _client(app) as client:
        await client.post(
            COMPLETIONS,
            json=_body("first", speaker="person:alex", speaker_confidence=0.9),
            headers=_bearer(VOICE_TOKEN),
        )
        # Too little audio to score: the front end still names a speaker, but the
        # score says do not trust it.
        await client.post(
            COMPLETIONS,
            json=_body("yes", speaker="person:guest", speaker_confidence=0.2),
            headers=_bearer(VOICE_TOKEN),
        )
    assert len(core.events) == 2
    assert core.events[1].handle.id == "alex"
    assert core.events[1].channel == voice_adapter.ROAMING_CHANNEL


@pytest.mark.asyncio
async def test_a_low_score_with_no_hold_falls_to_the_unknown_policy(tmp_path: Path) -> None:
    app, core, _ = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            COMPLETIONS,
            json=_body("hello", speaker="person:alex", speaker_confidence=0.1),
            headers=_bearer(VOICE_TOKEN),
        )
    assert core.events == []
    assert _content(_chunks(response.text)) == voice_adapter.IGNORE_REPLY


@pytest.mark.asyncio
async def test_a_name_with_a_low_score_is_never_resolved_by_the_guard(tmp_path: Path) -> None:
    """A claim Joshua does not trust must not become an identity.

    The front end names a real member, but the score says do not trust it. The
    roster lookup must not run again on that name, or an untrusted claim would
    walk straight in.
    """
    app, core, context = _make(tmp_path)
    async with _client(app) as client:
        await client.post(
            COMPLETIONS,
            json=_body("unlock the door", speaker="person:alex", speaker_confidence=0.1),
            headers=_bearer(VOICE_TOKEN),
        )
    assert core.events == []
    # The name is still in the audit ring, so an operator reads who spoke.
    assert context.guard.recent()[-1]["address"] == "alex"


@pytest.mark.asyncio
async def test_a_turn_with_no_speaker_at_all_is_refused(tmp_path: Path) -> None:
    app, core, context = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(COMPLETIONS, json=_body("hello"), headers=_bearer(VOICE_TOKEN))
    assert core.events == []
    assert _content(_chunks(response.text)) == voice_adapter.IGNORE_REPLY
    assert context.guard.recent()[-1]["address"] == UNNAMED_SPEAKER


# --- the request body -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_body_with_no_user_message_is_400(tmp_path: Path) -> None:
    app, core, _ = _make(tmp_path)
    async with _client(app) as client:
        response = await client.post(
            COMPLETIONS,
            json={"messages": [{"role": "system", "content": "hi"}]},
            headers=_bearer(VOICE_TOKEN),
        )
    assert response.status_code == 400
    assert core.events == []


@pytest.mark.asyncio
async def test_only_the_last_user_message_becomes_the_turn(tmp_path: Path) -> None:
    app, core, _ = _make(tmp_path)
    async with _client(app) as client:
        await client.post(
            COMPLETIONS,
            json={
                "messages": [
                    {"role": "user", "content": "what is the weather"},
                    {"role": "assistant", "content": "it is raining"},
                    {"role": "user", "content": "and tomorrow"},
                ],
                "user": "voice:office",
                "speaker": "person:alex",
            },
            headers=_bearer(VOICE_TOKEN),
        )
    assert core.events[0].text == "and tomorrow"


@pytest.mark.asyncio
async def test_text_over_the_cap_is_truncated(tmp_path: Path) -> None:
    config = CONFIG.replace(
        "unknown_sender: drop", 'unknown_sender: drop\n    max_text_chars: "10"'
    )
    app, core, _ = _make(tmp_path, config=config)
    async with _client(app) as client:
        await client.post(
            COMPLETIONS,
            json=_body("x" * 50, speaker="person:alex"),
            headers=_bearer(VOICE_TOKEN),
        )
    assert core.events[0].text == "x" * 10


# --- readiness --------------------------------------------------------------


@pytest.mark.asyncio
async def test_readyz_reports_the_voice_channel_with_no_names(tmp_path: Path) -> None:
    app, _core, _ = _make(tmp_path)
    async with _client(app) as client:
        await client.post(
            COMPLETIONS,
            json=_body("hello", speaker="person:alex", speaker_confidence=0.9),
            headers=_bearer(VOICE_TOKEN),
        )
        response = await client.get("/readyz")
    check = response.json()["checks"]["voice"]
    assert check == {"ok": True, "thread": "person", "held_identities": 1}


@pytest.mark.asyncio
async def test_readyz_has_no_voice_entry_when_the_channel_is_off(tmp_path: Path) -> None:
    app, _core, _ = _make(tmp_path, config=NO_VOICE_CONFIG)
    async with _client(app) as client:
        response = await client.get("/readyz")
    assert "voice" not in response.json()["checks"]


# --- the parts with no HTTP in them -----------------------------------------


def test_device_of_reads_both_forms() -> None:
    assert voice_adapter.device_of("voice:office") == "office"
    assert voice_adapter.device_of("office") == "office"
    assert voice_adapter.device_of("") == voice_adapter.DEFAULT_DEVICE
    assert voice_adapter.device_of(None) == voice_adapter.DEFAULT_DEVICE


def test_speaker_name_drops_the_person_prefix() -> None:
    assert voice_adapter.speaker_name("person:alex") == "alex"
    assert voice_adapter.speaker_name("alex") == "alex"
    assert voice_adapter.speaker_name(None) == ""


def test_confidence_of_reads_a_number_or_nothing() -> None:
    assert voice_adapter.confidence_of(0.87) == pytest.approx(0.87)
    assert voice_adapter.confidence_of("0.5") == pytest.approx(0.5)
    assert voice_adapter.confidence_of(None) is None
    assert voice_adapter.confidence_of("very sure") is None


def test_last_user_text_reads_content_parts() -> None:
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "set a "}, {"type": "text", "text": "timer"}],
        }
    ]
    assert voice_adapter.last_user_text(messages) == "set a timer"


def test_the_identity_hold_expires() -> None:
    now = [0.0]
    hold = IdentityHold(clock=lambda: now[0])
    hold.remember("office", "alex", 120)
    assert hold.held("office") == "alex"
    assert hold.count() == 1
    now[0] = 121.0
    assert hold.held("office") is None
    assert hold.count() == 0


def test_a_hold_of_zero_seconds_holds_nothing() -> None:
    hold = IdentityHold()
    hold.remember("office", "alex", 0)
    assert hold.held("office") is None
