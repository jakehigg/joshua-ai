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


def test_post_role_requires_admin(client) -> None:
    resp = client.post("/admin/people/alex/role", json={"role": "owner"})
    assert resp.status_code == 401
    resp = client.post(
        "/admin/people/alex/role",
        headers={"Authorization": f"Bearer {CHANNELS}"},
        json={"role": "owner"},
    )
    assert resp.status_code == 403
    assert client.repo.people["alex"].role == "member"


def test_post_role_changes_only_the_role(client) -> None:
    resp = client.post("/admin/people/alex/role", headers=_laptop(), json={"role": "owner"})
    assert resp.status_code == 200
    assert resp.json()["person"] == {"id": "alex", "name": "Alex", "role": "owner"}
    assert client.repo.people["alex"].role == "owner"
    assert client.repo.people["alex"].display_name == "Alex"
    assert client.repo.handles == {("telegram", "9"): "alex"}
    people = config_module.read_people_file(client.data_dir / "people.yaml")
    assert people == [{"id": "alex", "role": "owner"}]


def test_post_role_bad_role_is_400(client) -> None:
    resp = client.post("/admin/people/alex/role", headers=_laptop(), json={"role": "admin"})
    assert resp.status_code == 400
    assert client.repo.people["alex"].role == "member"


def test_post_role_bad_body_is_400(client) -> None:
    resp = client.post("/admin/people/alex/role", headers=_laptop(), json=["owner"])
    assert resp.status_code == 400


def test_post_role_unknown_person_is_404(client) -> None:
    resp = client.post("/admin/people/ghost/role", headers=_laptop(), json={"role": "owner"})
    assert resp.status_code == 404


def test_post_remove_requires_admin(client) -> None:
    resp = client.post("/admin/people/alex/remove")
    assert resp.status_code == 401
    resp = client.post("/admin/people/alex/remove", headers={"Authorization": f"Bearer {CHANNELS}"})
    assert resp.status_code == 403
    assert client.repo.people["alex"].role == "member"


def test_post_remove_drops_the_handles_and_the_secret(client) -> None:
    client.repo.secrets["alex"] = "$2b$04$hash"
    resp = client.post("/admin/people/alex/remove", headers=_laptop())
    assert resp.status_code == 200
    assert resp.json() == {"removed": True, "id": "alex"}
    assert client.repo.people["alex"].role == "removed"
    assert client.repo.handles == {}
    assert client.repo.secrets == {}
    people = config_module.read_people_file(client.data_dir / "people.yaml")
    assert people == [{"id": "alex", "role": "removed"}]


def test_post_remove_unknown_person_is_404(client) -> None:
    resp = client.post("/admin/people/ghost/remove", headers=_laptop())
    assert resp.status_code == 404


def test_get_people_lists_every_handle_of_a_person(client) -> None:
    client.repo.handles[("telegram", "10")] = "alex"
    client.repo.handles[("imessage", "+15550001111")] = "alex"
    resp = client.get("/admin/people", headers=_laptop())
    assert resp.status_code == 200
    handles = resp.json()["people"][0]["handles"]
    # One handle is a string, two on one channel are a list.
    assert handles == {"imessage": "+15550001111", "telegram": ["10", "9"]}


VIEWER = "viewer-token"


def _viewer(client, monkeypatch) -> dict[str, str]:
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)
    monkeypatch.delenv("AUTH_CALLERS", raising=False)
    return {"Authorization": f"Bearer {VIEWER}"}


def _with_owner(client) -> None:
    client.repo.people["sam"] = Person(id="sam", display_name="Sam", role="owner")


def test_the_viewer_reaches_the_roster_routes_with_no_admin_callers_change(
    client, monkeypatch
) -> None:
    """The viewer is in AUTH_CALLERS by default, so the roster routes admit it
    with the default ADMIN_CALLERS."""
    headers = _viewer(client, monkeypatch)
    assert client.get("/admin/people", headers=headers).status_code == 200
    resp = client.post(
        "/admin/people", headers=headers, json={"id": "iris", "name": "Iris", "role": "guest"}
    )
    assert resp.status_code == 201
    resp = client.post("/admin/people/iris/role", headers=headers, json={"role": "member"})
    assert resp.status_code == 200
    assert client.post("/admin/people/iris/remove", headers=headers).status_code == 200


def test_the_viewer_gets_no_other_admin_route(client, monkeypatch) -> None:
    """The roster is the only admin surface the viewer reaches."""
    headers = _viewer(client, monkeypatch)
    for path in ("/admin/pool", "/admin/kb/status", "/admin/snapshots"):
        assert client.get(path, headers=headers).status_code == 403, path


def test_an_empty_auth_callers_shuts_the_viewer_out_of_the_roster(client, monkeypatch) -> None:
    headers = _viewer(client, monkeypatch)
    monkeypatch.setenv("AUTH_CALLERS", "")
    assert client.get("/admin/people", headers=headers).status_code == 403


def test_post_people_with_no_handle_adds_a_person_with_none(client) -> None:
    resp = client.post("/admin/people", headers=_laptop(), json={"id": "iris", "name": "Iris"})
    assert resp.status_code == 201
    assert resp.json()["person"] == {"id": "iris", "name": "Iris", "role": "member", "handles": {}}
    assert client.repo.handles == {("telegram", "9"): "alex"}
    people = config_module.read_people_file(client.data_dir / "people.yaml")
    assert people == [{"id": "iris", "name": "Iris", "role": "member"}]


def test_post_people_with_a_bad_handle_is_400(client) -> None:
    resp = client.post(
        "/admin/people", headers=_laptop(), json={"id": "iris", "name": "Iris", "handle": "x"}
    )
    assert resp.status_code == 400
    assert "iris" not in client.repo.people


def test_the_only_owner_cannot_be_demoted(client) -> None:
    _with_owner(client)
    resp = client.post("/admin/people/sam/role", headers=_laptop(), json={"role": "member"})
    assert resp.status_code == 409
    assert "only owner" in resp.json()["error"]
    assert client.repo.people["sam"].role == "owner"
    assert not (client.data_dir / "people.yaml").exists()


def test_the_only_owner_cannot_be_demoted_through_an_add(client) -> None:
    _with_owner(client)
    resp = client.post(
        "/admin/people", headers=_laptop(), json={"id": "sam", "name": "Sam", "role": "member"}
    )
    assert resp.status_code == 409
    assert client.repo.people["sam"].role == "owner"


def test_the_only_owner_cannot_be_removed(client) -> None:
    _with_owner(client)
    client.repo.secrets["sam"] = "$2b$04$hash"
    resp = client.post("/admin/people/sam/remove", headers=_laptop())
    assert resp.status_code == 409
    assert client.repo.people["sam"].role == "owner"
    assert client.repo.secrets == {"sam": "$2b$04$hash"}


def test_an_owner_steps_down_once_there_is_a_second_owner(client) -> None:
    _with_owner(client)
    assert (
        client.post(
            "/admin/people/alex/role", headers=_laptop(), json={"role": "owner"}
        ).status_code
        == 200
    )
    resp = client.post("/admin/people/sam/role", headers=_laptop(), json={"role": "member"})
    assert resp.status_code == 200
    assert client.post("/admin/people/sam/remove", headers=_laptop()).status_code == 200


def test_a_roster_with_no_owner_still_changes(client) -> None:
    """The guard keeps an owner; it does not demand one where there is none."""
    resp = client.post("/admin/people/alex/role", headers=_laptop(), json={"role": "guest"})
    assert resp.status_code == 200
