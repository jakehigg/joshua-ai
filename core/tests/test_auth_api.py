"""`/auth/*`: who may call, what each route answers, and that no answer ever
carries a hash. A fake repo stands in for the database."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from joshua_core import auth_api
from joshua_core.auth_api import build_auth_router, hash_secret, verify_secret
from joshua_core.store.models import Person
from starlette.testclient import TestClient

VIEWER = "viewer-token"
MCP = "mcp-token"
LAPTOP = "laptop-token"
SECRET = "open sesame"


class FakeRepo:
    """The roster and the secrets, in memory."""

    def __init__(self, people: list[str], secrets: dict[str, str]) -> None:
        self.people = {p: Person(id=p, display_name=p.title(), role="member") for p in people}
        self.secrets = dict(secrets)
        self.stamps = {p: datetime(2026, 11, 1, 9, 0, tzinfo=UTC) for p in self.secrets}

    async def get_person(self, person_id: str) -> Person | None:
        return self.people.get(person_id)

    async def get_auth_secret(self, person_id: str) -> str | None:
        return self.secrets.get(person_id)

    async def set_auth_secret(self, person_id: str, secret_hash: str) -> None:
        self.secrets[person_id] = secret_hash
        self.stamps[person_id] = datetime(2026, 11, 2, 10, 30, tzinfo=UTC)

    async def delete_auth_secret(self, person_id: str) -> bool:
        self.stamps.pop(person_id, None)
        return self.secrets.pop(person_id, None) is not None

    async def list_auth_person_ids(self) -> list[str]:
        return sorted(self.secrets)

    async def get_auth_updated_at(self, person_id: str) -> datetime | None:
        return self.stamps.get(person_id)


@pytest.fixture(autouse=True)
def cheap_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lowest bcrypt cost. The tests hash often and check no timing."""
    monkeypatch.setattr(auth_api, "BCRYPT_ROUNDS", 4)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)
    monkeypatch.setenv("JOSHUA_TOKEN_MCP", MCP)
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP)
    monkeypatch.delenv("AUTH_CALLERS", raising=False)
    # `alex` has a secret, `mia` is on the roster with none.
    repo = FakeRepo(["alex", "mia"], {"alex": hash_secret(SECRET)})
    app = FastAPI()
    app.state.ctx = SimpleNamespace(repo=repo)
    app.include_router(build_auth_router())
    test_client = TestClient(app)
    test_client.repo = repo  # type: ignore[attr-defined]
    return test_client


def _call(
    client: TestClient, method: str, path: str, token: str | None = VIEWER, **body: Any
) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if method == "GET":
        return client.get(path, headers=headers)
    if method == "DELETE":
        return client.delete(path, headers=headers)
    return client.post(path, headers=headers, json=body)


# --- the hashing helpers ---------------------------------------------------


def test_a_hash_is_bcrypt_and_only_the_right_secret_verifies() -> None:
    hashed = hash_secret(SECRET)
    assert hashed.startswith("$2")
    assert SECRET not in hashed
    assert verify_secret(SECRET, hashed) is True
    assert verify_secret("wrong", hashed) is False
    assert verify_secret("", hashed) is False
    assert verify_secret(SECRET, None) is False
    assert verify_secret(SECRET, "not-a-hash") is False
    # The same secret hashed twice gives two hashes, and both verify.
    assert hash_secret(SECRET) != hashed
    assert verify_secret(SECRET, hash_secret(SECRET)) is True


# --- the allowlist ---------------------------------------------------------


ROUTES = (
    ("POST", "/auth/verify", {"person": "alex", "secret": SECRET}),
    ("POST", "/auth/secret", {"person": "alex", "secret": SECRET, "new_secret": "next"}),
    ("POST", "/auth/secret/admin", {"person": "alex", "new_secret": "next"}),
    ("DELETE", "/auth/secret/alex", {}),
    ("GET", "/auth/people", {}),
    ("GET", "/auth/people/alex", {}),
)


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_no_token_or_a_wrong_token_is_401(client, method, path, body) -> None:
    assert _call(client, method, path, token=None, **body).status_code == 401
    assert _call(client, method, path, token="not-a-token", **body).status_code == 401


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_a_caller_outside_auth_callers_is_403(client, method, path, body) -> None:
    assert _call(client, method, path, token=MCP, **body).status_code == 403
    assert _call(client, method, path, token=LAPTOP, **body).status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_auth_callers_names_the_callers(client, monkeypatch, method, path, body) -> None:
    monkeypatch.setenv("AUTH_CALLERS", "laptop")
    assert _call(client, method, path, token=VIEWER, **body).status_code == 403
    assert _call(client, method, path, token=LAPTOP, **body).status_code == 200


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_an_empty_list_turns_the_routes_off(client, monkeypatch, method, path, body) -> None:
    monkeypatch.setenv("AUTH_CALLERS", "")
    assert _call(client, method, path, token=VIEWER, **body).status_code == 403


# --- POST /auth/verify -----------------------------------------------------


def test_verify_takes_the_right_secret(client) -> None:
    response = _call(client, "POST", "/auth/verify", person="alex", secret=SECRET)
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_verify_refuses_a_wrong_secret(client) -> None:
    response = _call(client, "POST", "/auth/verify", person="alex", secret="guess")
    assert response.json() == {"ok": False}


def test_verify_answers_ok_false_for_a_person_with_no_secret(client) -> None:
    # `mia` is on the roster and has no secret: the answer is the same as for a
    # wrong secret, so it says nothing about her.
    response = _call(client, "POST", "/auth/verify", person="mia", secret=SECRET)
    assert response.status_code == 200
    assert response.json() == {"ok": False}


def test_verify_answers_ok_false_for_an_unknown_person(client) -> None:
    response = _call(client, "POST", "/auth/verify", person="nobody", secret=SECRET)
    assert response.status_code == 200
    assert response.json() == {"ok": False}


# --- POST /auth/secret -----------------------------------------------------


def test_a_person_changes_their_own_secret(client) -> None:
    response = _call(
        client, "POST", "/auth/secret", person="alex", secret=SECRET, new_secret="a new one"
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "person": "alex"}
    stored = client.repo.secrets["alex"]
    assert verify_secret("a new one", stored) is True
    assert verify_secret(SECRET, stored) is False


def test_a_wrong_current_secret_does_not_change_it(client) -> None:
    before = client.repo.secrets["alex"]
    response = _call(
        client, "POST", "/auth/secret", person="alex", secret="guess", new_secret="a new one"
    )
    assert response.status_code == 403
    assert client.repo.secrets["alex"] == before


def test_a_person_with_no_secret_cannot_change_one(client) -> None:
    response = _call(
        client, "POST", "/auth/secret", person="mia", secret=SECRET, new_secret="a new one"
    )
    assert response.status_code == 403
    assert "mia" not in client.repo.secrets


def test_changing_the_secret_of_an_unknown_person_is_404(client) -> None:
    response = _call(
        client, "POST", "/auth/secret", person="nobody", secret=SECRET, new_secret="a new one"
    )
    assert response.status_code == 404
    assert "nobody" not in client.repo.secrets


# --- POST /auth/secret/admin -----------------------------------------------


def test_the_admin_route_sets_a_first_secret(client) -> None:
    response = _call(client, "POST", "/auth/secret/admin", person="mia", new_secret="hers")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "person": "mia"}
    assert verify_secret("hers", client.repo.secrets["mia"]) is True


def test_the_admin_route_resets_a_secret_without_the_current_one(client) -> None:
    response = _call(client, "POST", "/auth/secret/admin", person="alex", new_secret="reset")
    assert response.status_code == 200
    assert verify_secret("reset", client.repo.secrets["alex"]) is True
    assert verify_secret(SECRET, client.repo.secrets["alex"]) is False


def test_the_admin_route_is_404_for_an_unknown_person(client) -> None:
    response = _call(client, "POST", "/auth/secret/admin", person="nobody", new_secret="x")
    assert response.status_code == 404
    assert "nobody" not in client.repo.secrets


# --- DELETE /auth/secret/{person_id} ---------------------------------------


def test_delete_drops_the_secret(client) -> None:
    response = _call(client, "DELETE", "/auth/secret/alex")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "person": "alex", "deleted": True}
    assert "alex" not in client.repo.secrets


def test_delete_of_a_person_with_no_secret_is_not_an_error(client) -> None:
    response = _call(client, "DELETE", "/auth/secret/mia")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "person": "mia", "deleted": False}


def test_delete_of_an_unknown_person_is_404(client) -> None:
    assert _call(client, "DELETE", "/auth/secret/nobody").status_code == 404


# --- GET /auth/people ------------------------------------------------------


def test_people_lists_only_the_people_with_a_secret(client) -> None:
    response = _call(client, "GET", "/auth/people")
    assert response.status_code == 200
    # `mia` is on the roster with no secret, so she is absent.
    assert response.json() == {"people": ["alex"]}

    _call(client, "POST", "/auth/secret/admin", person="mia", new_secret="hers")
    assert _call(client, "GET", "/auth/people").json() == {"people": ["alex", "mia"]}

    _call(client, "DELETE", "/auth/secret/alex")
    assert _call(client, "GET", "/auth/people").json() == {"people": ["mia"]}


def test_the_people_list_carries_no_time(client) -> None:
    """The viewer renders this list with no session, so when each person last
    changed their secret is not in it."""
    assert set(_call(client, "GET", "/auth/people").json()) == {"people"}


# --- GET /auth/people/{person_id} ------------------------------------------


def test_one_person_carries_when_their_secret_last_changed(client) -> None:
    response = _call(client, "GET", "/auth/people/alex")
    assert response.status_code == 200
    assert response.json() == {"person": "alex", "updated_at": "2026-11-01T09:00:00+00:00"}


def test_the_time_moves_when_the_secret_changes(client) -> None:
    before = _call(client, "GET", "/auth/people/alex").json()["updated_at"]
    _call(client, "POST", "/auth/secret", person="alex", secret=SECRET, new_secret="a new one")
    assert _call(client, "GET", "/auth/people/alex").json()["updated_at"] != before


def test_a_person_with_no_secret_is_404(client) -> None:
    # `mia` is on the roster with no secret: there is no time to give.
    response = _call(client, "GET", "/auth/people/mia")
    assert response.status_code == 404
    assert response.json() == {"error": "no secret"}


def test_a_person_who_is_not_on_the_roster_is_404(client) -> None:
    assert _call(client, "GET", "/auth/people/nobody").status_code == 404


# --- no answer carries a hash ----------------------------------------------


def test_no_answer_carries_a_secret_or_a_hash(client) -> None:
    stored = client.repo.secrets["alex"]
    bodies = [
        _call(client, "POST", "/auth/verify", person="alex", secret=SECRET).text,
        _call(client, "GET", "/auth/people").text,
        _call(client, "GET", "/auth/people/alex").text,
        _call(
            client, "POST", "/auth/secret", person="alex", secret=SECRET, new_secret="a new one"
        ).text,
        _call(client, "POST", "/auth/secret/admin", person="alex", new_secret="reset").text,
        _call(client, "DELETE", "/auth/secret/alex").text,
    ]
    for body in bodies:
        assert stored not in body
        assert "$2" not in body
        assert "secret_hash" not in body
        assert SECRET not in body
        assert json.loads(body)  # every answer is JSON


def test_the_router_names_the_documented_routes() -> None:
    router = build_auth_router()
    assert {(r.path, tuple(sorted(r.methods))) for r in router.routes} == {
        ("/auth/verify", ("POST",)),
        ("/auth/secret", ("POST",)),
        ("/auth/secret/admin", ("POST",)),
        ("/auth/secret/{person_id}", ("DELETE",)),
        ("/auth/people", ("GET",)),
        ("/auth/people/{person_id}", ("GET",)),
    }
