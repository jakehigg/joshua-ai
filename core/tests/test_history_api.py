"""Offline tests for ``POST /v1/history/search`` and ``POST /v1/history/read``.

A fake repo holds two conversations of ``alex``, a shared workspace thread,
and the rows of each. The real queries run in ``test_store.py``. What these
tests check is the scope: which conversation ids the route asks the store for.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from joshua_core.history_api import (
    HISTORY_READ_PATH,
    HISTORY_SEARCH_PATH,
    build_history_router,
    scope_of,
)
from joshua_core.main import build_app
from joshua_core.store.models import Conversation, Workspace
from joshua_core.store.transcript import TranscriptHit, TranscriptPage, TranscriptRow
from starlette.testclient import TestClient

ASSIGNMENTS = "assign-tok"
VIEWER = "viewer-tok"

BASE = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)

SHARED = Workspace(
    id="11111111-1111-4111-8111-111111111111",
    slug="thanksgiving",
    title="Thanksgiving",
    owner="alex",
    shared=True,
    members=("mia",),
)
PERSONAL = Workspace(
    id="22222222-2222-4222-8222-222222222222", slug="solar", title="Solar", owner="alex"
)


class FakeRepo:
    """The conversations of ``alex``, plus the thread of the shared workspace.

    ``alex`` owns ``dm-alex`` and the thread of the personal workspace. The
    shared thread has no person, so it is in nobody's own list.
    """

    def __init__(self) -> None:
        self.own = {"alex": ["dm-alex", "ws-solar"], "mia": ["dm-mia"]}
        self.rows = {
            "dm-alex": "a receipt of the market",
            "ws-solar": "a receipt of the panels",
            "ws-shared": "a receipt of the dinner",
            "dm-mia": "a receipt of mia",
        }
        self.searched: list[tuple[list[str], str, int]] = []
        self.read: list[list[str]] = []
        self.created = 0

    async def person_conversation_ids(self, person_id: str) -> list[str]:
        return list(self.own.get(person_id, ()))

    async def find_workspace_for(self, person_id: str, ref: str) -> Any:
        for workspace in (SHARED, PERSONAL):
            if person_id in workspace.people and ref in (workspace.id, workspace.slug):
                return workspace
        return None

    async def find_conversation(self, channel_id: str, person_id: str | None) -> Any:
        if channel_id == SHARED.channel_id:
            return Conversation(id="ws-shared", channel_id=channel_id, person_id=None)
        return None

    async def get_or_create_conversation(self, channel_id: str, person_id: str | None) -> Any:
        self.created += 1
        raise AssertionError("a history route must never create a conversation")

    async def search_transcript(
        self, conversation_ids: Any, query: str, limit: int = 10
    ) -> list[TranscriptHit]:
        ids = list(conversation_ids)
        self.searched.append((ids, query, limit))
        return [
            TranscriptHit(
                id=i,
                conversation_id=cid,
                direction="in",
                speaker="Alex",
                created_at=BASE,
                snippet=self.rows[cid],
            )
            for i, cid in enumerate(ids, start=1)
            if query in self.rows[cid]
        ]

    async def read_transcript(
        self,
        conversation_ids: Any,
        start: datetime,
        end: datetime,
        *,
        cursor: str | None = None,
        max_rows: int = 60,
        max_chars: int = 12_000,
    ) -> TranscriptPage:
        ids = list(conversation_ids)
        self.read.append(ids)
        if cursor == "bad":
            raise ValueError("the cursor is not valid")
        rows = [
            TranscriptRow(
                id=i,
                conversation_id=cid,
                direction="out" if i % 2 else "in",
                speaker=None,
                created_at=BASE,
                content=self.rows[cid],
                cut=False,
            )
            for i, cid in enumerate(ids, start=1)
        ]
        return TranscriptPage(rows=rows, cursor="next-1" if rows else None)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("JOSHUA_TOKEN_ASSIGNMENTS", ASSIGNMENTS)
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)
    app = FastAPI()
    app.state.ctx = SimpleNamespace(repo=FakeRepo())
    app.include_router(build_history_router())
    tc = TestClient(app)
    tc.repo = app.state.ctx.repo  # type: ignore[attr-defined]
    return tc


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _search(client: TestClient, token: str | None = ASSIGNMENTS, **body: Any) -> Any:
    headers = _bearer(token) if token else {}
    return client.post(
        HISTORY_SEARCH_PATH, json={"person": "alex", "query": "receipt", **body}, headers=headers
    )


def _read(client: TestClient, token: str | None = ASSIGNMENTS, **body: Any) -> Any:
    headers = _bearer(token) if token else {}
    return client.post(
        HISTORY_READ_PATH,
        json={"person": "alex", "start": "2026-03-04T00:00:00+00:00", **body},
        headers=headers,
    )


def test_the_core_app_serves_both_routes() -> None:
    paths = build_app().openapi()["paths"]
    assert HISTORY_SEARCH_PATH in paths and HISTORY_READ_PATH in paths


# --- the scope ---------------------------------------------------------------


def test_a_person_run_reads_that_persons_conversations(client: TestClient) -> None:
    body = _search(client).json()
    assert client.repo.searched == [(["dm-alex", "ws-solar"], "receipt", 10)]
    assert [m["text"] for m in body["messages"]] == [
        "a receipt of the market",
        "a receipt of the panels",
    ]
    # Never another person's conversation, and never a shared thread.
    assert "dm-mia" not in client.repo.searched[0][0]
    assert "ws-shared" not in client.repo.searched[0][0]


def test_a_shared_workspace_run_reads_that_thread_only(client: TestClient) -> None:
    body = _search(client, workspace=SHARED.id).json()
    assert client.repo.searched == [(["ws-shared"], "receipt", 10)]
    assert [m["text"] for m in body["messages"]] == ["a receipt of the dinner"]
    assert client.repo.created == 0


def test_a_shared_workspace_run_reads_the_thread_by_its_slug(client: TestClient) -> None:
    _search(client, workspace="thanksgiving")
    assert client.repo.searched == [(["ws-shared"], "receipt", 10)]


def test_a_personal_workspace_run_reads_the_owners_conversations(client: TestClient) -> None:
    """A personal thread is a conversation of its owner, so the scope is theirs."""
    _search(client, workspace="solar")
    assert client.repo.searched == [(["dm-alex", "ws-solar"], "receipt", 10)]


def test_a_workspace_the_person_does_not_reach_has_an_empty_scope(client: TestClient) -> None:
    for workspace in (SHARED.id, "solar", "nothing"):
        body = _search(client, person="robin", workspace=workspace).json()
        assert body["messages"] == [], workspace
    assert client.repo.searched == [([], "receipt", 10)] * 3


def test_a_person_with_no_conversation_has_an_empty_scope(client: TestClient) -> None:
    assert _search(client, person="robin").json()["messages"] == []
    assert client.repo.searched == [([], "receipt", 10)]


def test_the_read_route_takes_the_same_scope(client: TestClient) -> None:
    assert _read(client).status_code == 200
    assert _read(client, workspace=SHARED.id).status_code == 200
    assert client.repo.read == [["dm-alex", "ws-solar"], ["ws-shared"]]


async def test_scope_of_a_shared_workspace_with_no_thread_is_empty() -> None:
    repo = FakeRepo()
    repo.find_conversation = _no_conversation  # type: ignore[method-assign]
    assert await scope_of(repo, "alex", SHARED.id) == []


async def _no_conversation(channel_id: str, person_id: str | None) -> None:
    return None


# --- the answer --------------------------------------------------------------


def test_a_search_message_holds_the_turn_as_it_is(client: TestClient) -> None:
    message = _search(client).json()["messages"][0]
    assert message == {
        "at": "2026-03-04T12:00:00+00:00",
        "direction": "in",
        "speaker": "Alex",
        "text": "a receipt of the market",
        "cut": False,
    }


def test_a_read_page_holds_the_cursor_of_the_next_page(client: TestClient) -> None:
    body = _read(client).json()
    assert body["cursor"] == "next-1"
    assert [m["direction"] for m in body["messages"]] == ["out", "in"]


def test_the_limit_of_a_search_goes_to_the_store(client: TestClient) -> None:
    assert _search(client, limit=3).status_code == 200
    assert client.repo.searched[0][2] == 3


def test_the_end_of_a_read_defaults_to_now(client: TestClient) -> None:
    assert _read(client, end=None).status_code == 200
    assert client.repo.read == [["dm-alex", "ws-solar"]]


# --- bad requests ------------------------------------------------------------


def test_a_search_with_no_query_is_a_bad_request(client: TestClient) -> None:
    response = client.post(
        HISTORY_SEARCH_PATH, json={"person": "alex"}, headers=_bearer(ASSIGNMENTS)
    )
    assert response.status_code == 400
    assert "query" in response.json()["error"]
    assert client.repo.searched == []


def test_a_limit_over_the_maximum_is_a_bad_request(client: TestClient) -> None:
    assert _search(client, limit=500).status_code == 400


def test_a_person_that_is_not_a_slug_is_a_bad_request(client: TestClient) -> None:
    assert _search(client, person="Alex Smith").status_code == 400


def test_an_end_before_the_start_is_a_bad_request(client: TestClient) -> None:
    response = _read(client, end="2026-03-03T00:00:00+00:00")
    assert response.status_code == 400
    assert client.repo.read == []


def test_a_read_with_no_start_is_a_bad_request(client: TestClient) -> None:
    response = client.post(HISTORY_READ_PATH, json={"person": "alex"}, headers=_bearer(ASSIGNMENTS))
    assert response.status_code == 400


def test_a_cursor_that_is_not_valid_is_a_bad_request(client: TestClient) -> None:
    response = _read(client, cursor="bad")
    assert response.status_code == 400
    assert response.json() == {"error": "the cursor is not valid"}


def test_a_time_with_no_offset_is_read_as_utc(client: TestClient) -> None:
    assert _read(client, start="2026-03-04T00:00:00", end="2026-03-04T23:00:00").status_code == 200


# --- auth --------------------------------------------------------------------


def test_the_assignments_container_may_read(client: TestClient) -> None:
    assert _search(client, ASSIGNMENTS).status_code == 200
    assert _read(client, ASSIGNMENTS).status_code == 200


def test_the_viewer_is_forbidden(client: TestClient) -> None:
    assert _search(client, VIEWER).status_code == 403
    assert _read(client, VIEWER).status_code == 403
    assert client.repo.searched == [] and client.repo.read == []


def test_no_token_is_unauthorized(client: TestClient) -> None:
    assert _search(client, None).status_code == 401
    assert _read(client, None).status_code == 401


def test_a_wrong_token_is_unauthorized(client: TestClient) -> None:
    assert _search(client, "not-a-token").status_code == 401
