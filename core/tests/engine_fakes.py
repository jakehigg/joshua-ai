"""Offline fakes for the engine unit tests.

They stand in for the store repo, the prompt composer, and the profile deriver
so the manager runs with no DB, no SDK, and no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from joshua_core.store.models import Channel, Conversation, Person
from joshua_shared import config as config_module

BASE_CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    role: member
  - id: mia
    name: Mia
    role: member
"""


def make_settings(*, pool_max: int = 20, idle_seconds: int = 1800, mcp: str = "") -> Any:
    text = (
        BASE_CONFIG + f"\ncore:\n  pool_max: {pool_max}\n  session_idle_seconds: {idle_seconds}\n"
    )
    if mcp:
        text += mcp
    return config_module.parse(text, env={}, source="<test>")


class FakeRepo:
    """Records transcript writes and serves the two people the manager reads."""

    def __init__(self) -> None:
        self.transcripts: list[dict[str, Any]] = []
        self.sdk_sessions: dict[str, str] = {}
        self.touched: list[str] = []
        self._people = {
            "alex": Person(id="alex", display_name="Alex", role="member"),
            "mia": Person(id="mia", display_name="Mia", role="member"),
        }

    async def get_person(self, person_id: str) -> Person | None:
        return self._people.get(person_id)

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

    async def set_sdk_session(
        self, conversation_id: str, session_id: str, last_channel_id: str | None = None
    ) -> None:
        self.sdk_sessions[conversation_id] = session_id

    async def touch_conversation(
        self, conversation_id: str, last_channel_id: str | None = None
    ) -> None:
        self.touched.append(conversation_id)


class FakeComposer:
    def compose(self, profile: Any, person: Any, channel: Channel, memory: Any = None) -> str:
        return "you are joshua"


def fake_derive_profile(person: Any, channel: Channel) -> Any:
    return SimpleNamespace(name="default", model=None, max_turns=0)


def make_channel(channel_id: str = "telegram:1", session_mode: str = "per_person") -> Channel:
    return Channel(id=channel_id, channel_type="telegram", session_mode=session_mode)


def make_conversation(
    conversation_id: str = "c1",
    person_id: str | None = "alex",
    sdk_session_id: str | None = None,
    channel_id: str = "telegram:1",
) -> Conversation:
    return Conversation(
        id=conversation_id,
        channel_id=channel_id,
        person_id=person_id,
        sdk_session_id=sdk_session_id,
    )


# --- fake Claude Agent SDK -------------------------------------------------
# Enough of the SDK surface to drive the manager's SDK backend offline.


class FakeSDKOptions:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _StreamEvent:
    def __init__(self, text: str):
        self.event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}


class ResultMessage:
    def __init__(self):
        self.result = "Hello world"
        self.session_id = "sess-9"
        self.usage = {"input_tokens": 1, "output_tokens": 2}
        self.total_cost_usd = 0.01
        self.is_error = False


class FakeSDKClient:
    def __init__(self, options):
        self.options = options

    async def connect(self):
        return None

    async def get_mcp_status(self):
        return {"mcpServers": [{"name": "files", "status": "connected"}]}

    async def query(self, prompt):
        return None

    async def receive_response(self):
        yield _StreamEvent("Hello ")
        yield ResultMessage()

    async def interrupt(self):
        return None

    async def disconnect(self):
        return None


def install_fake_sdk(monkeypatch) -> None:
    from joshua_core.engine import agent

    monkeypatch.setattr(agent, "ClaudeAgentOptions", FakeSDKOptions)
    monkeypatch.setattr(agent, "ClaudeSDKClient", FakeSDKClient)
