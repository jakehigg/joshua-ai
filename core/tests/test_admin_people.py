"""Tests for the core admin people routes and their ADMIN_CALLERS gate."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from joshua_core.admin import build_admin_router
from joshua_core.store.models import Person
from joshua_shared import config as config_module
from people_fakes import PeopleFakeRepo
from starlette.testclient import TestClient

LAPTOP = "laptop-token"
CHANNELS = "channels-token"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP)
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", CHANNELS)
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    repo = PeopleFakeRepo([Person(id="alex", display_name="Alex", role="member")])
    repo.handles[("telegram", "9")] = "alex"
    app = FastAPI()
    app.state.ctx = SimpleNamespace(repo=repo)
    app.include_router(build_admin_router())
    tc = TestClient(app)
    tc.repo = repo  # type: ignore[attr-defined]
    tc.data_dir = tmp_path  # type: ignore[attr-defined]
    return tc


def _laptop() -> dict[str, str]:
    return {"Authorization": f"Bearer {LAPTOP}"}


def test_get_people_requires_bearer(client) -> None:
    assert client.get("/admin/people").status_code == 401


def test_get_people_forbids_non_admin(client) -> None:
    # channels authenticates but is not in ADMIN_CALLERS.
    resp = client.get("/admin/people", headers={"Authorization": f"Bearer {CHANNELS}"})
    assert resp.status_code == 403


def test_get_people_lists_roster(client) -> None:
    resp = client.get("/admin/people", headers=_laptop())
    assert resp.status_code == 200
    people = resp.json()["people"]
    assert people == [
        {"id": "alex", "name": "Alex", "role": "member", "handles": {"telegram": "9"}}
    ]


def test_post_people_adds(client) -> None:
    resp = client.post(
        "/admin/people",
        headers=_laptop(),
        json={"id": "remy", "name": "Remy", "handle": "telegram:555", "role": "guest"},
    )
    assert resp.status_code == 201
    assert resp.json()["person"]["id"] == "remy"
    assert client.repo.people["remy"].role == "guest"
    people = config_module.read_people_file(client.data_dir / "people.yaml")
    assert people[0]["id"] == "remy"


def test_post_people_conflict_is_400(client) -> None:
    resp = client.post(
        "/admin/people",
        headers=_laptop(),
        json={"id": "remy", "name": "Remy", "handle": "telegram:9"},
    )
    assert resp.status_code == 400
    assert "already belongs" in resp.json()["error"]


def test_post_people_bad_body_is_400(client) -> None:
    resp = client.post("/admin/people", headers=_laptop(), json=["not", "an", "object"])
    assert resp.status_code == 400
