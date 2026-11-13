"""Offline tests for ``GET /api/v1/session``: the person who signed in, the
sections of their navigation, the chat address and the timezone. Same fixture
shape as ``test_viewer.py``."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from conftest import bad_cookie, signed_in
from joshua_gateway import viewer, viewer_api
from joshua_gateway.viewer_api import session as session_api
from joshua_shared import config
from starlette.testclient import TestClient

ALEX = signed_in("alex")
MIA = signed_in("mia")
SAM = signed_in("sam")

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
      - id: mia
        name: Mia
        role: guest
      - id: sam
        name: Sam
        role: owner
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
        mia: ${VIEWER_PW_MIA:-}
        sam: ${VIEWER_PW_SAM:-}
    """)

# ``dedent`` already took the indent off, so the line of the viewer block
# carries two spaces and the key goes in beside it.
CONFIG_ANON = CONFIG.replace("  enabled: true\n", "  enabled: true\n  anonymous: true\n")


def _data(monkeypatch, tmp_path: Path, text: str) -> Path:
    data = tmp_path / "data"
    (data / "wiki").mkdir(parents=True)
    (data / "wiki/Home.md").write_text("# Home\n\nThe front door.\n")
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(text)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    return data


@pytest.fixture
def data(monkeypatch, tmp_path) -> Path:
    return _data(monkeypatch, tmp_path, CONFIG)


@pytest.fixture
def client(data) -> TestClient:
    return TestClient(viewer.build_app())


@pytest.fixture
def anon_client(monkeypatch, tmp_path) -> TestClient:
    _data(monkeypatch, tmp_path, CONFIG_ANON)
    return TestClient(viewer.build_app())


def test_the_session_of_a_member_holds_every_section(client) -> None:
    body = client.get("/api/v1/session", auth=ALEX).json()
    assert body["person"] == {"id": "alex", "name": "Alex", "role": "member"}
    assert body["anonymous"] is False
    assert [item["key"] for item in body["sections"]] == [
        "wiki",
        "journal",
        "search",
        "usage",
        "assignments",
        "documents",
        "chat",
    ]
    assert {"key": "wiki", "label": "Wiki", "path": "/wiki/"} in body["sections"]
    assert body["timezone"] == "America/New_York"


def test_the_chat_section_names_the_person(client) -> None:
    body = client.get("/api/v1/session", auth=ALEX).json()
    assert body["chat_url"] == "/chat/alex"
    assert {"key": "chat", "label": "Chat", "path": "/chat/alex"} in body["sections"]


def test_a_guest_has_no_documents_section(client) -> None:
    body = client.get("/api/v1/session", auth=MIA).json()
    assert body["person"]["role"] == "guest"
    assert "documents" not in {item["key"] for item in body["sections"]}
    assert body["chat_url"] == "/chat/mia"


def test_an_owner_holds_every_section_a_member_holds(client) -> None:
    """An owner is a member with the roster in hand, so no section is missing."""
    body = client.get("/api/v1/session", auth=SAM).json()
    assert body["person"] == {"id": "sam", "name": "Sam", "role": "owner"}
    assert [item["key"] for item in body["sections"]] == [
        item["key"] for item in client.get("/api/v1/session", auth=ALEX).json()["sections"]
    ]


def test_the_anonymous_person_is_named_as_such(anon_client) -> None:
    body = anon_client.get("/api/v1/session").json()
    assert body["person"] == {"id": "anonymous", "name": "Anonymous", "role": "member"}
    assert body["anonymous"] is True
    assert "documents" in {item["key"] for item in body["sections"]}


def test_the_links_of_the_sidebar_are_not_in_the_session(client, data) -> None:
    """``GET /api/v1/links`` is the one source of the list."""
    (data / "wiki/.viewer").mkdir(parents=True)
    (data / "wiki/.viewer/links.yaml").write_text(
        "links:\n  - title: Pantry\n    url: https://pantry.example.test/\n    icon: box\n"
    )
    assert "links" not in client.get("/api/v1/session", auth=ALEX).json()


def test_the_session_is_json_and_never_cached(client) -> None:
    response = client.get("/api/v1/session", auth=ALEX)
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"


def test_the_session_needs_a_sign_in(client) -> None:
    response = client.get("/api/v1/session")
    assert response.status_code == 401
    assert "WWW-Authenticate" not in response.headers
    assert client.get("/api/v1/session", auth=bad_cookie()).status_code == 401


def test_every_json_route_is_under_api_v1(data) -> None:
    """One constant holds the prefix, so no route can drift from it."""
    paths = [route.path for route in viewer_api.routes()]
    assert paths
    assert all(path.startswith(f"{viewer_api.API_PREFIX}/") for path in paths)
    assert viewer_api.API_PREFIX == "/api/v1"
    assert viewer_api.api_path("/wiki/tree") == "/api/v1/wiki/tree"


def test_the_person_json_holds_no_handle(data) -> None:
    person = config.load().person("alex")
    assert person is not None
    assert set(session_api.person_json(person)) == {"id", "name", "role"}
