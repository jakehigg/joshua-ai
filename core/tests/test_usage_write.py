"""`POST /v1/usage`: who may write usage rows, which rows core accepts, and
that a written row shows in `GET /v1/usage`. A fake repo stands in for the
database."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from joshua_core.store.usage_events import UsageEvent
from joshua_core.usage_api import build_usage_router
from joshua_shared.config import UsageConfig
from joshua_shared.contracts import (
    USAGE_WRITE_MAX_BYTES,
    USAGE_WRITE_MAX_ROWS,
    USAGE_WRITE_PATH,
)
from starlette.testclient import TestClient

ASSIGNMENTS = "assignments-token"
VIEWER = "viewer-token"
CORE = "core-token"
NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


class FakeRepo:
    """Keeps the written rows, one for each ref, and sums them by name and
    worker for `usage_rows`, as the database does."""

    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[UsageEvent] = []
        self.fail = fail

    async def add_usage_event(self, event: UsageEvent) -> None:
        if self.fail:
            raise RuntimeError("database is down")
        if any(e.ref == event.ref for e in self.events):
            return
        self.events.append(event)

    async def usage_rows(self, since: datetime, timezone: str) -> list[dict[str, Any]]:
        return [
            {
                "day": NOW.date(),
                "name": e.name,
                "worker": e.worker,
                "model": e.model,
                "calls": e.calls,
                "input": e.usage.input,
                "output": e.usage.output,
                "cache_read": e.usage.cache_read,
                "cache_write": e.usage.cache_write,
            }
            for e in self.events
        ]


def _client(monkeypatch, *, writers=("assignments",), repo: FakeRepo | None = None) -> TestClient:
    monkeypatch.setenv("JOSHUA_TOKEN_ASSIGNMENTS", ASSIGNMENTS)
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE)
    settings = SimpleNamespace(
        timezone="America/New_York",
        usage=UsageConfig(writers=list(writers)),
    )
    app = FastAPI()
    app.state.ctx = SimpleNamespace(settings=settings, repo=repo or FakeRepo())
    app.include_router(build_usage_router(clock=lambda: NOW))
    client = TestClient(app)
    client.repo = app.state.ctx.repo  # type: ignore[attr-defined]
    return client


def _row(**fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "kind": "assignment",
        "name": "assignment",
        "worker": "assignment-12",
        "run_id": 12,
        "person_id": "alex",
        "model": "claude-sonnet-5",
        "endpoint": "anthropic",
        "calls": 3,
        "usage": {"input": 100, "output": 20, "cache_read": 1000, "cache_write": 50},
        "ref": "12:slice:1",
    }
    row.update(fields)
    return row


def _post(client: TestClient, token: str | None, body: Any) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(USAGE_WRITE_PATH, headers=headers, json=body)


# ── who may write ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("token", [None, "not-a-token"])
def test_no_token_or_a_wrong_token_is_401(monkeypatch, token: str | None) -> None:
    client = _client(monkeypatch)
    assert _post(client, token, {"rows": [_row()]}).status_code == 401
    assert client.repo.events == []  # type: ignore[attr-defined]


@pytest.mark.parametrize("token", [VIEWER, CORE])
def test_an_identity_off_the_writers_is_403(monkeypatch, token: str) -> None:
    client = _client(monkeypatch)
    assert _post(client, token, {"rows": [_row()]}).status_code == 403
    assert client.repo.events == []  # type: ignore[attr-defined]


def test_an_empty_writers_list_turns_the_write_off(monkeypatch) -> None:
    client = _client(monkeypatch, writers=())
    assert _post(client, ASSIGNMENTS, {"rows": [_row()]}).status_code == 403


def test_the_bearer_is_checked_before_the_body(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert _post(client, None, {"rows": [_row(kind="chat")]}).status_code == 401


# ── which rows core accepts ──────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["chat", "reflection", "internet", "describe", "voice", ""])
def test_a_row_that_is_not_an_assignment_is_400(monkeypatch, kind: str) -> None:
    client = _client(monkeypatch)
    response = _post(client, ASSIGNMENTS, {"rows": [_row(), _row(kind=kind, ref="other")]})
    assert response.status_code == 400
    assert client.repo.events == []  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "chat"},
        {"person_id": "Not A Person"},
        {"usage": {"input": -1}},
        {"calls": 2**31},
        {"ref": ""},
        {"ref": "bad\nref"},
        {"worker": "x" * 65},
        {"extra": "field"},
    ],
)
def test_a_row_that_is_not_valid_is_400(monkeypatch, fields: dict[str, Any]) -> None:
    client = _client(monkeypatch)
    response = _post(client, ASSIGNMENTS, {"rows": [_row(**fields)]})
    assert response.status_code == 400
    assert client.repo.events == []  # type: ignore[attr-defined]


def test_a_body_that_is_not_json_is_400(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.post(
        USAGE_WRITE_PATH,
        headers={"Authorization": f"Bearer {ASSIGNMENTS}"},
        content=b"not json",
    )
    assert response.status_code == 400


def test_too_many_rows_is_400(monkeypatch) -> None:
    client = _client(monkeypatch)
    rows = [_row(ref=f"r{n}") for n in range(USAGE_WRITE_MAX_ROWS + 1)]
    assert _post(client, ASSIGNMENTS, {"rows": rows}).status_code == 400
    assert client.repo.events == []  # type: ignore[attr-defined]


def test_a_body_over_the_limit_is_413(monkeypatch) -> None:
    client = _client(monkeypatch)
    body = json.dumps({"rows": [], "pad": "x" * USAGE_WRITE_MAX_BYTES}).encode()
    response = client.post(
        USAGE_WRITE_PATH, headers={"Authorization": f"Bearer {ASSIGNMENTS}"}, content=body
    )
    assert response.status_code == 413


def test_a_streamed_body_over_the_limit_is_413(monkeypatch) -> None:
    client = _client(monkeypatch)

    def parts() -> Any:
        yield b'{"rows": [], "pad": "'
        for _ in range(USAGE_WRITE_MAX_BYTES // 65536 + 1):
            yield b"x" * 65536
        yield b'"}'

    response = client.post(
        USAGE_WRITE_PATH, headers={"Authorization": f"Bearer {ASSIGNMENTS}"}, content=parts()
    )
    assert response.status_code == 413


# ── a written row ────────────────────────────────────────────────────────────


def test_an_assignment_row_is_written_and_shows_in_the_usage(monkeypatch, caplog) -> None:
    client = _client(monkeypatch)
    with caplog.at_level(logging.INFO):
        response = _post(client, ASSIGNMENTS, {"rows": [_row()]})
    assert response.status_code == 200
    assert response.json() == {"accepted": 1}

    [event] = client.repo.events  # type: ignore[attr-defined]
    assert (event.kind, event.name) == ("assignment", "assignment")
    assert event.run_id == 12
    assert event.worker == "assignment-12"
    assert event.person_id == "alex"
    assert event.model == "claude-sonnet-5"
    assert event.endpoint == "anthropic"
    assert event.calls == 3
    assert (event.usage.input, event.usage.output) == (100, 20)
    assert (event.usage.cache_read, event.usage.cache_write) == (1000, 50)
    # The ref holds the writer, so a writer cannot reach another writer's rows.
    assert event.ref == "assignments:12:slice:1"
    logged = "\n".join(f"{r.msg!r}" for r in caplog.records)
    assert "claude-sonnet-5" not in logged and "alex" not in logged

    usage = client.get("/v1/usage", headers={"Authorization": f"Bearer {VIEWER}"}).json()
    assert "assignment" in usage["groups"]
    group = usage["totals"]["assignment"]
    assert group["calls"] == 3
    assert group["total"] == 1170
    assert group["workers"]["assignment-12"]["calls"] == 3


def test_a_row_sent_again_is_not_written_again(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert _post(client, ASSIGNMENTS, {"rows": [_row()]}).status_code == 200
    assert _post(client, ASSIGNMENTS, {"rows": [_row()]}).status_code == 200
    assert len(client.repo.events) == 1  # type: ignore[attr-defined]


def test_the_assignment_group_is_there_with_no_row(monkeypatch) -> None:
    client = _client(monkeypatch)
    usage = client.get("/v1/usage", headers={"Authorization": f"Bearer {VIEWER}"}).json()
    assert "assignment" in usage["groups"]
    assert usage["totals"]["assignment"]["calls"] == 0


def test_an_empty_list_writes_nothing(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = _post(client, ASSIGNMENTS, {"rows": []})
    assert response.json() == {"accepted": 0}


def test_a_database_failure_is_503(monkeypatch) -> None:
    client = _client(monkeypatch, repo=FakeRepo(fail=True))
    response = _post(client, ASSIGNMENTS, {"rows": [_row()]})
    assert response.status_code == 503
    assert "database" not in response.text
