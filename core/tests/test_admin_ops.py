"""Offline tests for the operability admin routes (pool, flush, transcript, turn).

These use fake manager and repo stand-ins so the routes run with no DB or SDK.
The full path against real Postgres lives in ``test_smoke_integration.py``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from joshua_core.admin import build_admin_router
from joshua_core.store.models import Channel, Conversation
from starlette.testclient import TestClient

LAPTOP = "laptop-token"
CORE = "core-token"


class FakeManager:
    def __init__(self) -> None:
        self.flush_calls = 0
        self.turns: list[dict[str, Any]] = []

    async def admin_pool(self) -> dict[str, Any]:
        return {
            "sessions": [
                {
                    "conversation_id": "conv-1",
                    "person_id": "alex",
                    "profile": "dm",
                    "idle_s": 1.5,
                    "locked": False,
                }
            ],
            "max": 20,
        }

    async def flush_sessions(self) -> int:
        self.flush_calls += 1
        return 3

    async def run_turn(
        self,
        channel: Channel,
        conversation: Conversation,
        text: str,
        *,
        framing: str | None = None,
        person_id: str | None = None,
        direction: str = "in",
    ) -> Any:
        self.turns.append(
            {"channel": channel.id, "person_id": person_id, "text": text, "framing": framing}
        )
        return SimpleNamespace(text=f"echo:{text}")


class FakeRepo:
    def __init__(self) -> None:
        self.channels = {
            "telegram:1": Channel(
                id="telegram:1",
                channel_type="telegram",
                session_mode="per_person",
                default_person_id="alex",
            )
        }
        self.tail = [
            {
                "direction": "in",
                "content": "hi",
                "status": "ok",
                "meta": {},
                "created_at": datetime(2026, 8, 26, 12, 0, 0),
            },
            {
                "direction": "out",
                "content": "hello",
                "status": "ok",
                "meta": {},
                "created_at": datetime(2026, 8, 26, 12, 0, 1),
            },
        ]
        self.tail_limit: int | None = None

    async def resolve_channel(self, ref: str) -> Channel | None:
        return self.channels.get(ref)

    async def get_or_create_conversation(
        self, channel_id: str, person_id: str | None
    ) -> Conversation:
        return Conversation(id="conv-1", channel_id=channel_id, person_id=person_id)

    async def transcript_tail(self, conversation_id: str, limit: int = 40) -> list[dict[str, Any]]:
        self.tail_limit = limit
        return self.tail


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP)
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE)
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    app = FastAPI()
    app.state.ctx = SimpleNamespace(manager=FakeManager(), repo=FakeRepo())
    app.include_router(build_admin_router())
    tc = TestClient(app)
    tc.ctx = app.state.ctx  # type: ignore[attr-defined]
    return tc


def _laptop() -> dict[str, str]:
    return {"Authorization": f"Bearer {LAPTOP}"}


def test_pool_requires_admin(client) -> None:
    assert client.get("/admin/pool").status_code == 401
    assert client.get("/admin/pool", headers={"Authorization": f"Bearer {CORE}"}).status_code == 403


def test_pool_returns_sessions_and_max(client) -> None:
    body = client.get("/admin/pool", headers=_laptop()).json()
    assert body["max"] == 20
    assert body["sessions"][0]["profile"] == "dm"


def test_flush_returns_count(client) -> None:
    resp = client.post("/admin/sessions/flush", headers=_laptop())
    assert resp.status_code == 200
    assert resp.json() == {"flushed": 3}
    assert client.ctx.manager.flush_calls == 1


def test_transcript_serializes_timestamps_and_clamps_limit(client) -> None:
    resp = client.get("/admin/transcript/conv-1?limit=9999", headers=_laptop())
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert [r["direction"] for r in rows] == ["in", "out"]
    assert rows[0]["created_at"] == "2026-08-26T12:00:00"
    # 9999 is clamped to the maximum tail size.
    assert client.ctx.repo.tail_limit == 500


def test_transcript_bad_limit_uses_default(client) -> None:
    client.get("/admin/transcript/conv-1?limit=abc", headers=_laptop())
    assert client.ctx.repo.tail_limit == 40


def test_turn_runs_and_returns_text(client) -> None:
    resp = client.post(
        "/admin/turn",
        headers=_laptop(),
        json={"channel": "telegram:1", "person": "alex", "text": "ping"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"conversation_id": "conv-1", "text": "echo:ping"}
    turn = client.ctx.manager.turns[0]
    assert turn == {"channel": "telegram:1", "person_id": "alex", "text": "ping", "framing": None}


def test_turn_defaults_person_to_channel_default(client) -> None:
    resp = client.post(
        "/admin/turn", headers=_laptop(), json={"channel": "telegram:1", "text": "ping"}
    )
    assert resp.status_code == 200
    assert client.ctx.manager.turns[0]["person_id"] == "alex"


def test_turn_unknown_channel_is_404(client) -> None:
    resp = client.post(
        "/admin/turn", headers=_laptop(), json={"channel": "telegram:nope", "text": "ping"}
    )
    assert resp.status_code == 404


def test_turn_requires_channel_and_text(client) -> None:
    assert client.post("/admin/turn", headers=_laptop(), json={"text": "hi"}).status_code == 400
    assert (
        client.post("/admin/turn", headers=_laptop(), json={"channel": "telegram:1"}).status_code
        == 400
    )


def test_turn_bad_body_is_400(client) -> None:
    assert client.post("/admin/turn", headers=_laptop(), json=["nope"]).status_code == 400


def test_ops_routes_require_admin(client) -> None:
    assert client.post("/admin/sessions/flush").status_code == 401
    assert client.get("/admin/transcript/conv-1").status_code == 401
    turn = client.post("/admin/turn", json={"channel": "telegram:1", "text": "x"})
    assert turn.status_code == 401
