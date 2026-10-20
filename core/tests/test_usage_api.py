"""`GET /v1/usage` and `GET /admin/usage`: who may call them, the window, and
the shape of the answer. A fake repo stands in for the database."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from joshua_core.usage_api import ADMIN_USAGE_PATH, USAGE_PATH, build_usage_router
from joshua_shared.config import Jobs, UsageConfig
from starlette.testclient import TestClient

VIEWER = "viewer-token"
MCP = "mcp-token"
LAPTOP = "laptop-token"
NOW = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)


class FakeRepo:
    """Answers `usage_rows` with fixed rows and records the window it got."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[datetime, str]] = []

    async def usage_rows(self, since: datetime, timezone: str) -> list[dict[str, Any]]:
        self.calls.append((since, timezone))
        return list(self.rows)


ROWS = [
    {
        "day": date(2026, 10, 20),
        "name": "chat",
        "worker": None,
        "calls": 12,
        "input": 100,
        "output": 50,
        "cache_read": 1000,
        "cache_write": 10,
    },
    {
        "day": date(2026, 10, 19),
        "name": "curator",
        "worker": "review",
        "model": "m-curator",
        "calls": 3,
        "input": 30,
        "output": 3,
        "cache_read": 0,
        "cache_write": 0,
    },
]


def _client(monkeypatch, *, allowed=("viewer",), rows=ROWS, jobs: Jobs | None = None) -> TestClient:
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)
    monkeypatch.setenv("JOSHUA_TOKEN_MCP", MCP)
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP)
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    repo = FakeRepo(rows)
    settings = SimpleNamespace(
        timezone="America/New_York",
        usage=UsageConfig(allowed_callers=list(allowed)),
        jobs=jobs or Jobs(),
    )
    app = FastAPI()
    app.state.ctx = SimpleNamespace(settings=settings, repo=repo)
    app.include_router(build_usage_router(clock=lambda: NOW))
    client = TestClient(app)
    client.repo = repo  # type: ignore[attr-defined]
    return client


def _get(client: TestClient, token: str | None, path: str = USAGE_PATH, **params: Any):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.get(path, headers=headers, params=params)


def test_no_token_or_a_wrong_token_is_401(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert _get(client, None).status_code == 401
    assert _get(client, "not-a-token").status_code == 401
    assert client.repo.calls == []  # type: ignore[attr-defined]


def test_an_identity_off_the_list_is_403(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert _get(client, MCP).status_code == 403


def test_an_empty_list_turns_the_route_off(monkeypatch) -> None:
    client = _client(monkeypatch, allowed=())
    assert _get(client, VIEWER).status_code == 403
    assert client.repo.calls == []  # type: ignore[attr-defined]


def test_the_answer_has_the_documented_shape(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = _get(client, VIEWER)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"timezone", "days", "groups", "rows", "totals"}
    assert body["timezone"] == "America/New_York"
    assert body["days"] == 30
    assert body["groups"] == ["chat", "reflection", "internet", "describe", "curator"]
    assert [r["date"] for r in body["rows"]] == ["2026-10-20", "2026-10-19"]
    chat = body["rows"][0]["groups"]["chat"]
    assert chat == {
        "calls": 12,
        "input": 100,
        "output": 50,
        "cache_read": 1000,
        "cache_write": 10,
        "total": 1160,
        "models": [],
        "workers": {},
    }
    assert body["totals"]["curator"]["workers"]["review"]["calls"] == 3
    assert body["totals"]["curator"]["models"] == ["m-curator"]
    assert body["totals"]["curator"]["workers"]["review"]["models"] == ["m-curator"]
    assert body["totals"]["reflection"]["total"] == 0
    assert set(body["totals"]) == set(body["groups"])
    [(since, timezone)] = client.repo.calls  # type: ignore[attr-defined]
    assert timezone == "America/New_York"
    assert since.isoformat() == "2026-09-21T00:00:00-04:00"


def test_days_is_clamped(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert _get(client, VIEWER, days=0).json()["days"] == 1
    assert _get(client, VIEWER, days=5000).json()["days"] == 366
    since = client.repo.calls[0][0]  # type: ignore[attr-defined]
    assert since.date() == date(2026, 10, 20)


def test_days_that_is_not_an_integer_is_422(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert _get(client, VIEWER, days="ten").status_code == 422
    assert _get(client, VIEWER, days="1.5").status_code == 422


def test_the_admin_route_takes_an_admin_caller_only(monkeypatch) -> None:
    client = _client(monkeypatch, allowed=())
    assert _get(client, None, ADMIN_USAGE_PATH).status_code == 401
    assert _get(client, VIEWER, ADMIN_USAGE_PATH).status_code == 403
    response = _get(client, LAPTOP, ADMIN_USAGE_PATH, days=7)
    assert response.status_code == 200
    assert response.json()["days"] == 7


def test_the_groups_list_every_feature_and_each_job_that_is_on(monkeypatch) -> None:
    jobs = Jobs.model_validate(
        {
            "enabled": True,
            "curator": {"enabled": True},
            "research": {"enabled": True},
            "insights": {"enabled": False},
        }
    )
    rows = [{**ROWS[0], "name": "bundle"}]
    client = _client(monkeypatch, rows=rows, jobs=jobs)
    body = _get(client, VIEWER).json()
    assert body["groups"] == [
        "chat",
        "reflection",
        "internet",
        "describe",
        "curator",
        "research",
        "bundle",
    ]
    assert body["totals"]["research"]["calls"] == 0
    assert body["totals"]["research"]["workers"] == {}
    assert list(body["rows"][0]["groups"]) == ["bundle"]


def test_a_job_that_is_on_is_not_listed_when_jobs_are_off(monkeypatch) -> None:
    jobs = Jobs.model_validate({"enabled": False, "research": {"enabled": True}})
    client = _client(monkeypatch, rows=[], jobs=jobs)
    body = _get(client, VIEWER).json()
    assert body["groups"] == ["chat", "reflection", "internet", "describe"]
    assert body["rows"] == []
