"""Offline tests for ``GET`` and ``POST /api/v1/profile``.

A fake core stands in for ``/people/{id}/profile`` and the stamp route through
a MockTransport. The routes act on the signed-in person only.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import signed_in
from joshua_gateway import viewer, viewer_session
from joshua_shared import config
from starlette.testclient import TestClient

ALEX = signed_in("alex")
MIA = signed_in("mia")
STAMP = "2026-11-01T09:00:00+00:00"
CORE_URL = "http://core.test"
VIEWER_TOKEN = "viewer-token"
ORIGIN = {"Origin": "http://testserver", "Host": "testserver"}

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
      - id: mia
        name: Mia
        role: guest
    viewer:
      enabled: true
      auth:
        method: password
    """)


class FakeCore:
    def __init__(self) -> None:
        self.profiles: dict[str, dict[str, Any]] = {
            "alex": {"prompt": None, "journal": "auto", "in_config": True},
            "mia": {"prompt": "Short.", "journal": "ask", "in_config": False},
        }
        self.requests: list[httpx.Request] = []
        self.unreachable = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.unreachable:
            raise httpx.ConnectError("down", request=request)
        parts = request.url.path.strip("/").split("/")
        if parts[:2] == ["auth", "people"]:
            return httpx.Response(200, json={"person": parts[2], "updated_at": STAMP})
        assert parts[0] == "people" and parts[2] == "profile"
        profile = self.profiles.get(parts[1])
        if profile is None:
            return httpx.Response(404, json={"error": "unknown person"})
        if request.method == "POST":
            body = json.loads(request.content)
            if body.get("journal", "auto") not in ("auto", "ask", "off"):
                return httpx.Response(
                    400, json={"error": "journal must be 'auto', 'ask', or 'off'"}
                )
            profile.update(body)
        return httpx.Response(200, json=profile)

    def profile_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if "/profile" in r.url.path]


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch) -> None:
    monkeypatch.setattr(viewer_session, "_STAMPS", {})


@pytest.fixture
def core(monkeypatch) -> FakeCore:
    fake = FakeCore()
    transport = httpx.MockTransport(fake.handler)

    def from_env(env: Any) -> viewer_session.CoreAuth | None:
        if not env.get(viewer_session.CORE_URL_ENV) or not env.get(viewer_session.VIEWER_TOKEN_ENV):
            return None
        return viewer_session.CoreAuth(CORE_URL, VIEWER_TOKEN, transport=transport)

    monkeypatch.setenv(viewer_session.CORE_URL_ENV, CORE_URL)
    monkeypatch.setenv(viewer_session.VIEWER_TOKEN_ENV, VIEWER_TOKEN)
    monkeypatch.setattr(viewer_session, "from_env", from_env)
    return fake


def _data(monkeypatch, tmp_path: Path) -> None:
    data = tmp_path / "data"
    (data / "wiki").mkdir(parents=True)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)


@pytest.fixture
def client(monkeypatch, tmp_path, core) -> TestClient:
    _data(monkeypatch, tmp_path)
    return TestClient(viewer.build_app())


def test_both_routes_need_a_session(client, core) -> None:
    assert client.get("/api/v1/profile").status_code == 401
    assert (
        client.post("/api/v1/profile", json={"journal": "off"}, headers=ORIGIN).status_code == 401
    )
    assert core.profile_calls() == []


def test_a_read_answers_the_profile_of_the_signed_in_person(client, core) -> None:
    response = client.get("/api/v1/profile", auth=ALEX)
    assert response.status_code == 200
    assert response.json() == {"prompt": None, "journal": "auto", "in_config": True}
    [call] = core.profile_calls()
    assert call.url.path == "/people/alex/profile"
    assert call.headers["authorization"] == f"Bearer {VIEWER_TOKEN}"


def test_a_write_acts_on_the_own_id_only(client, core) -> None:
    response = client.post(
        "/api/v1/profile",
        json={
            "prompt": "Be kind.",
            "journal": "off",
            "id": "mia",
            "person": "mia",
            "role": "owner",
        },
        headers=ORIGIN,
        auth=ALEX,
    )
    assert response.status_code == 200
    assert response.json() == {"prompt": "Be kind.", "journal": "off", "in_config": True}
    [call] = core.profile_calls()
    assert call.url.path == "/people/alex/profile"
    assert json.loads(call.content) == {"prompt": "Be kind.", "journal": "off"}
    assert core.profiles["mia"]["prompt"] == "Short."


def test_a_guest_edits_their_own_profile(client, core) -> None:
    response = client.post("/api/v1/profile", json={"journal": "off"}, headers=ORIGIN, auth=MIA)
    assert response.status_code == 200
    assert core.profiles["mia"]["journal"] == "off"
    assert core.profile_calls()[0].url.path == "/people/mia/profile"


def test_core_refusal_of_a_bad_value_is_400(client) -> None:
    response = client.post("/api/v1/profile", json={"journal": "never"}, headers=ORIGIN, auth=ALEX)
    assert response.status_code == 400
    assert "journal" in response.json()["error"]


def test_a_write_from_another_origin_is_refused(client, core) -> None:
    response = client.post(
        "/api/v1/profile",
        json={"journal": "off"},
        headers={"Origin": "http://evil.test", "Host": "testserver"},
        auth=ALEX,
    )
    assert response.status_code == 403
    assert core.profile_calls() == []


def test_a_body_that_is_not_an_object_is_refused(client, core) -> None:
    response = client.post("/api/v1/profile", json=["x"], headers=ORIGIN, auth=ALEX)
    assert response.status_code == 403
    assert core.profile_calls() == []


def test_core_down_is_503(client, core) -> None:
    core.unreachable = True
    # The cookie stamp is cached for the process, so the session still stands.
    viewer_session._STAMPS["alex"] = (float("inf"), STAMP)
    assert client.get("/api/v1/profile", auth=ALEX).status_code == 503
    post = client.post("/api/v1/profile", json={"journal": "off"}, headers=ORIGIN, auth=ALEX)
    assert post.status_code == 503


def test_an_unknown_person_in_core_is_404(client, core) -> None:
    core.profiles.pop("alex")
    assert client.get("/api/v1/profile", auth=ALEX).status_code == 404


def test_no_core_configured_is_503(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(viewer_session.CORE_URL_ENV, raising=False)
    monkeypatch.delenv(viewer_session.VIEWER_TOKEN_ENV, raising=False)
    _data(monkeypatch, tmp_path)
    client = TestClient(viewer.build_app())
    assert client.get("/api/v1/profile", auth=ALEX).status_code == 503
    post = client.post("/api/v1/profile", json={"journal": "off"}, headers=ORIGIN, auth=ALEX)
    assert post.status_code == 503
