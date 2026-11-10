"""Offline tests for ``GET /v1/transcript``.

A fake repo holds the transcript in memory and pages it the way
``Repo.transcript_page`` does. The real query runs in ``test_store.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from joshua_core.main import build_app
from joshua_core.store.models import Conversation
from joshua_core.store.repo import TRANSCRIPT_PAGE_MAX
from joshua_core.transcript_api import (
    SAFE_META_KEYS,
    TRANSCRIPT_PATH,
    build_transcript_router,
    message,
    safe_meta,
)
from starlette.testclient import TestClient

CHANNELS = "chan-tok"
LAPTOP = "laptop-tok"
GATEWAY = "gw-tok"

BASE = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)


class FakeRepo:
    """One conversation, ``cli:alex`` for ``alex``, with ``rows`` in id order."""

    def __init__(self, count: int = 5) -> None:
        self.conversation = Conversation(id="conv-1", channel_id="cli:alex", person_id="alex")
        self.rows: list[dict[str, Any]] = []
        for i in range(1, count + 1):
            self.rows.append(
                {
                    "id": i,
                    "direction": "in" if i % 2 else "out",
                    "content": f"message {i}",
                    "status": "ok",
                    "meta": {"turn_id": f"t{i}"},
                    "created_at": BASE + timedelta(seconds=i),
                }
            )
        self.lookups: list[tuple[str, str | None]] = []
        self.created = 0

    async def find_conversation(self, channel_id: str, person_id: str | None) -> Any:
        self.lookups.append((channel_id, person_id))
        if (channel_id, person_id) == ("cli:alex", "alex"):
            return self.conversation
        return None

    async def get_or_create_conversation(self, channel_id: str, person_id: str | None) -> Any:
        self.created += 1
        raise AssertionError("the transcript route must never create a conversation")

    async def transcript_page(
        self, conversation_id: str, limit: int = 50, before: int | None = None
    ) -> tuple[list[dict[str, Any]], int | None]:
        limit = max(1, min(limit, TRANSCRIPT_PAGE_MAX))
        older = [r for r in self.rows if before is None or r["id"] < before]
        page = older[-limit:]
        more = len(older) > limit
        return page, (page[0]["id"] if more and page else None)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", CHANNELS)
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP)
    monkeypatch.setenv("JOSHUA_TOKEN_GATEWAY", GATEWAY)
    app = FastAPI()
    app.state.ctx = SimpleNamespace(repo=FakeRepo())
    app.include_router(build_transcript_router())
    tc = TestClient(app)
    tc.repo = app.state.ctx.repo  # type: ignore[attr-defined]
    return tc


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _get(client: TestClient, token: str | None = CHANNELS, **params: Any) -> Any:
    query = {"channel": "cli:alex", "person": "alex", **params}
    headers = _bearer(token) if token else {}
    return client.get(TRANSCRIPT_PATH, params=query, headers=headers)


def test_the_core_app_serves_the_route() -> None:
    assert TRANSCRIPT_PATH in build_app().openapi()["paths"]


# --- auth ------------------------------------------------------------------


def test_channels_may_read(client: TestClient) -> None:
    assert _get(client, CHANNELS).status_code == 200


def test_laptop_may_read(client: TestClient) -> None:
    assert _get(client, LAPTOP).status_code == 200


def test_gateway_is_forbidden(client: TestClient) -> None:
    response = _get(client, GATEWAY)
    assert response.status_code == 403
    assert client.repo.lookups == []


def test_no_token_is_unauthorized(client: TestClient) -> None:
    response = _get(client, None)
    assert response.status_code == 401
    assert client.repo.lookups == []


def test_a_wrong_token_is_unauthorized(client: TestClient) -> None:
    assert _get(client, "not-a-token").status_code == 401


# --- query -----------------------------------------------------------------


def test_a_missing_channel_is_a_bad_request(client: TestClient) -> None:
    response = client.get(TRANSCRIPT_PATH, params={"person": "alex"}, headers=_bearer(CHANNELS))
    assert response.status_code == 400
    assert response.json() == {"reason": "bad_request"}


def test_a_blank_channel_is_a_bad_request(client: TestClient) -> None:
    assert _get(client, channel="  ").status_code == 400


@pytest.mark.parametrize("limit", ["0", "201", "-1", "many"])
def test_a_bad_limit_is_422(client: TestClient, limit: str) -> None:
    assert _get(client, limit=limit).status_code == 422


@pytest.mark.parametrize("before", ["0", "-5", "abc"])
def test_a_bad_before_is_422(client: TestClient, before: str) -> None:
    assert _get(client, before=before).status_code == 422


def test_an_unknown_conversation_is_empty(client: TestClient) -> None:
    response = _get(client, channel="cli:nobody", person="nobody")
    assert response.status_code == 200
    assert response.json() == {"conversation_id": None, "messages": [], "next_before": None}
    assert client.repo.created == 0


def test_no_person_looks_up_the_group_conversation(client: TestClient) -> None:
    response = client.get(
        TRANSCRIPT_PATH, params={"channel": "telegram:-100"}, headers=_bearer(CHANNELS)
    )
    assert response.status_code == 200
    assert response.json()["messages"] == []
    assert client.repo.lookups == [("telegram:-100", None)]


# --- reply -----------------------------------------------------------------


def test_messages_are_oldest_first_with_the_reply_shape(client: TestClient) -> None:
    body = _get(client).json()
    assert body["conversation_id"] == "conv-1"
    assert [m["id"] for m in body["messages"]] == [1, 2, 3, 4, 5]
    assert body["next_before"] is None
    first = body["messages"][0]
    assert first == {
        "id": 1,
        "direction": "in",
        "text": "message 1",
        "status": "ok",
        "at": "2026-03-04T12:00:01+00:00",
        "meta": {"turn_id": "t1"},
    }


def test_before_pages_back_to_the_start(client: TestClient) -> None:
    first = _get(client, limit=2).json()
    assert [m["id"] for m in first["messages"]] == [4, 5]
    assert first["next_before"] == 4

    second = _get(client, limit=2, before=first["next_before"]).json()
    assert [m["id"] for m in second["messages"]] == [2, 3]
    assert second["next_before"] == 2

    last = _get(client, limit=2, before=second["next_before"]).json()
    assert [m["id"] for m in last["messages"]] == [1]
    assert last["next_before"] is None


def test_the_route_drops_a_meta_key_that_is_not_safe(client: TestClient) -> None:
    client.repo.rows[0]["meta"] = {
        "turn_id": "t1",
        "tools": ["search_memory"],
        "cost_usd": 0.12,
        "usage": {"input_tokens": 10},
        "written": ["wiki/journal/2026/03/04/entry.md"],
        "speaker": "998877",
    }
    meta = _get(client).json()["messages"][0]["meta"]
    assert meta == {"turn_id": "t1", "tools": ["search_memory"]}


# --- helpers ---------------------------------------------------------------


def test_safe_meta_keeps_only_the_listed_keys() -> None:
    full = {key: 1 for key in SAFE_META_KEYS}
    full.update({"lock_wait_ms": 3, "write_failed": ["x"], "event": True})
    assert safe_meta(full) == {key: 1 for key in SAFE_META_KEYS}


def test_safe_meta_of_a_value_that_is_not_a_dict_is_empty() -> None:
    assert safe_meta(None) == {}
    assert safe_meta(["turn_id"]) == {}


def test_message_renders_a_time_in_utc() -> None:
    eastern = timezone(timedelta(hours=-5))
    row = {
        "id": 7,
        "direction": "out",
        "content": "hi",
        "status": None,
        "meta": {},
        "created_at": datetime(2026, 3, 4, 7, 0, 0, tzinfo=eastern),
    }
    rendered = message(row)
    assert rendered["at"] == "2026-03-04T12:00:00+00:00"
    assert rendered["status"] == "ok"


def test_message_takes_a_naive_time_as_utc() -> None:
    row = {"id": 1, "direction": "in", "content": "x", "created_at": datetime(2026, 3, 4)}
    assert message(row)["at"] == "2026-03-04T00:00:00+00:00"


def test_message_with_no_time_has_no_at() -> None:
    row = {"id": 1, "direction": "in", "content": "x"}
    assert message(row)["at"] is None
