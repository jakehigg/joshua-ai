"""`GET /admin/snapshots`: the state of each snapshot for an operator, no text."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from joshua_core.admin import build_admin_router
from joshua_shared.config import Memory, Snapshot
from starlette.testclient import TestClient

LAPTOP = "laptop-token"


class Repo:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def list_snapshot_results(self) -> list[dict]:
        return self.rows


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP)
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", "channels-token")
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    snaps = [
        Snapshot(name="open-tasks", server="tasks", tool="list_items", every="5m", scope="alex"),
        Snapshot(name="never-ran", server="tasks", tool="list_items", every="1h"),
    ]
    rows = [
        {
            "name": "open-tasks",
            "person_id": "alex",
            "fetched_at": datetime.now(UTC) - timedelta(minutes=1),
            "ok": False,
            "text": json.dumps(["private one", "private two"]),
            "sha256": "abc",
            "failures": 1,
            "last_error": "gateway 502",
        }
    ]
    app = FastAPI()
    app.state.ctx = SimpleNamespace(
        settings=SimpleNamespace(memory=Memory(snapshots=snaps)), repo=Repo(rows)
    )
    app.include_router(build_admin_router())
    return TestClient(app)


def test_needs_an_admin_caller(client) -> None:
    assert client.get("/admin/snapshots").status_code == 401
    denied = client.get("/admin/snapshots", headers={"Authorization": "Bearer channels-token"})
    assert denied.status_code == 403


def test_lists_each_snapshot_without_text(client) -> None:
    response = client.get("/admin/snapshots", headers={"Authorization": f"Bearer {LAPTOP}"})
    assert response.status_code == 200
    first, second = response.json()["snapshots"]
    assert first["name"] == "open-tasks"
    assert first["server"] == "tasks"
    assert first["tool"] == "list_items"
    assert first["scope"] == "alex"
    assert first["every"] == "5m"
    assert first["ok"] is False
    assert first["failures"] == 1
    assert first["last_error"] == "gateway 502"
    assert first["items"] == 2
    assert first["listed"] is True
    assert first["fetched_at"]
    assert second == {
        "name": "never-ran",
        "server": "tasks",
        "tool": "list_items",
        "scope": "shared",
        "every": "1h",
        "fetched_at": None,
        "ok": False,
        "failures": 0,
        "last_error": None,
        "items": 0,
        "listed": False,
    }
    assert "private" not in response.text
