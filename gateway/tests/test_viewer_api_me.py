"""Offline tests for ``GET /api/v1/me``: the person, their profile address and
their recent attachments. Same fixture shape as ``test_viewer.py``."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from joshua_gateway import viewer
from joshua_gateway.viewer_api import me as me_api
from joshua_shared import config
from starlette.requests import Request
from starlette.testclient import TestClient

ALEX = ("alex", "alex-secret")
MIA = ("mia", "mia-secret")

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
      users:
        alex: ${VIEWER_PW_ALEX:-}
        mia: ${VIEWER_PW_MIA:-}
    """)

CONFIG_ANON = CONFIG.replace(
    "      enabled: true\n", "      enabled: true\n      anonymous: true\n"
)


def _data(monkeypatch, tmp_path: Path, text: str) -> Path:
    data = tmp_path / "data"
    (data / "wiki/people").mkdir(parents=True)
    (data / "wiki/people/alex.md").write_text("# Alex\n\nA member.\n")
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(text)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
    monkeypatch.setenv("VIEWER_PW_MIA", MIA[1])
    return data


def _attachment(data: Path, rel: str, text: str = "paid\n") -> None:
    path = data / "people/alex/attachments" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


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


def test_the_page_of_a_member_holds_the_profile_and_the_attachments(client, data) -> None:
    _attachment(data, "2026/08/receipt.txt")
    body = client.get("/api/v1/me", auth=ALEX).json()
    assert body["person"] == {"id": "alex", "name": "Alex", "role": "member"}
    assert body["profile_url"] == "/wiki/people/alex.md"
    assert body["attachments"] == [
        {"path": "2026/08/receipt.txt", "url": "/attachments/2026/08/receipt.txt"}
    ]


def test_no_attachments_folder_gives_an_empty_list(client) -> None:
    body = client.get("/api/v1/me", auth=ALEX).json()
    assert body["attachments"] == []
    assert body["profile_url"] == "/wiki/people/alex.md"


def test_the_newest_attachments_come_first_and_the_list_is_capped(client, data) -> None:
    for month in range(1, 13):
        for day in (1, 2, 3):
            _attachment(data, f"2026/{month:02d}/note-{day}.txt")
    body = client.get("/api/v1/me", auth=ALEX).json()
    assert len(body["attachments"]) == viewer.HOME_ATTACHMENTS_MAX
    assert body["attachments"][0]["path"] == "2026/12/note-3.txt"


def test_a_metadata_file_is_no_attachment(client, data) -> None:
    _attachment(data, "2026/08/receipt.txt")
    _attachment(data, "2026/08/receipt.txt.meta.json", "{}\n")
    body = client.get("/api/v1/me", auth=ALEX).json()
    assert [item["path"] for item in body["attachments"]] == ["2026/08/receipt.txt"]


def test_a_guest_reads_their_own_page(client) -> None:
    body = client.get("/api/v1/me", auth=MIA).json()
    assert body["person"]["role"] == "guest"
    assert body["profile_url"] == "/wiki/people/mia.md"


def test_the_anonymous_person_has_no_profile_and_no_attachment(anon_client, tmp_path) -> None:
    _attachment(tmp_path / "data", "2026/08/receipt.txt")
    body = anon_client.get("/api/v1/me").json()
    assert body["person"]["id"] == viewer.ANONYMOUS_PERSON_ID
    assert body["profile_url"] is None
    assert body["attachments"] == []


async def test_the_handler_reads_the_person_of_the_request(data) -> None:
    """The handler takes ``(request, person)`` and reads the person only."""
    person = config.load().person("alex")
    assert person is not None
    request = Request({"type": "http", "method": "GET", "path": "/api/v1/me", "headers": []})
    response = await me_api.me(request, person)
    assert json.loads(response.body)["person"]["id"] == "alex"


def test_the_page_is_json_and_never_cached(client) -> None:
    response = client.get("/api/v1/me", auth=ALEX)
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"


def test_the_page_needs_a_sign_in(client) -> None:
    response = client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")
