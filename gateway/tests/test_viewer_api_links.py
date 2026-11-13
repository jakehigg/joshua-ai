"""Offline tests for the JSON routes of the links.

``GET /api/v1/links`` gives the list, the built-in icons, and the token of
the write, which only a member gets. ``POST /api/v1/links`` replaces the whole
list in ``wiki/.viewer/links.yaml``: it wants the member role, the token of
the ``links`` scope, and an origin of the host.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest
from conftest import ViewerSession, signed_in
from joshua_gateway import viewer
from joshua_gateway import viewer_links as links_mod
from joshua_gateway.viewer_api import links as links_api
from joshua_shared import config, wikigit
from starlette.testclient import TestClient

_GIT_MISSING = shutil.which("git") is None

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

PANTRY = "https://pantry.example.test/"
VINYL = "http://vinyl.test:8080/"


@pytest.fixture
def data(monkeypatch, tmp_path) -> Path:
    data = tmp_path / "data"
    (data / "wiki").mkdir(parents=True)
    (data / "wiki/Home.md").write_text("# Home\n\nThe front door.\n")
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    return data


@pytest.fixture
def client(data) -> TestClient:
    return TestClient(viewer.build_app())


def _links_file(data: Path) -> Path:
    return data / "wiki/.viewer/links.yaml"


def _seed(data: Path, text: str) -> None:
    path = _links_file(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _one_link(data: Path) -> None:
    _seed(data, f"links:\n  - title: Pantry\n    url: {PANTRY}\n    icon: box\n")


def _token(person_id: str = "alex") -> str:
    return links_api._csrf_token(person_id, links_api.LINKS_CSRF)


def _post(
    client: TestClient,
    rows: list[dict[str, str]],
    auth: ViewerSession = ALEX,
    csrf: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    body = {"csrf": _token(auth.person) if csrf is None else csrf, "links": rows}
    return client.post("/api/v1/links", json=body, auth=auth, headers=headers)


# -- the read -------------------------------------------------------------------


def test_a_member_gets_the_list_the_icons_and_the_token(client, data) -> None:
    _one_link(data)
    body = client.get("/api/v1/links", auth=ALEX).json()
    assert body["links"] == [{"title": "Pantry", "url": PANTRY, "icon": "box"}]
    assert body["icons"] == list(links_mod.ICONS)
    assert body["icons"][0] == links_mod.DEFAULT_ICON
    assert body["can_edit"] is True
    assert body["csrf"] == _token()


def test_an_owner_gets_the_token_and_writes(client, data) -> None:
    """The owner role passes the member gate of the read and of the write."""
    _one_link(data)
    body = client.get("/api/v1/links", auth=SAM).json()
    assert body["can_edit"] is True
    assert body["csrf"] == _token("sam")
    response = _post(client, [{"title": "Vinyl", "url": VINYL, "icon": "music"}], auth=SAM)
    assert response.status_code == 200
    assert links_mod.load(data / "wiki") == [links_mod.Link("Vinyl", VINYL, "music")]


def test_a_guest_gets_the_list_and_no_token(client, data) -> None:
    _one_link(data)
    body = client.get("/api/v1/links", auth=MIA).json()
    assert body["links"] == [{"title": "Pantry", "url": PANTRY, "icon": "box"}]
    assert body["can_edit"] is False
    assert body["csrf"] is None


def test_with_no_file_the_list_is_empty(client) -> None:
    body = client.get("/api/v1/links", auth=ALEX).json()
    assert body["links"] == []
    assert body["can_edit"] is True


def test_the_list_is_json_and_never_cached(client) -> None:
    response = client.get("/api/v1/links", auth=ALEX)
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"


def test_the_list_needs_a_sign_in(client) -> None:
    assert client.get("/api/v1/links").status_code == 401


# -- the write ------------------------------------------------------------------


def test_a_write_replaces_the_whole_list(client, data) -> None:
    _one_link(data)
    response = _post(
        client,
        [
            {"title": "  Vinyl  ", "url": VINYL, "icon": "music"},
            {"title": "", "url": "", "icon": "link"},
            {"title": "Pantry", "url": PANTRY, "icon": "cart"},
        ],
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "links": [
            {"title": "Vinyl", "url": VINYL, "icon": "music"},
            {"title": "Pantry", "url": PANTRY, "icon": "cart"},
        ],
    }
    assert links_mod.load(data / "wiki") == [
        links_mod.Link("Vinyl", VINYL, "music"),
        links_mod.Link("Pantry", PANTRY, "cart"),
    ]
    assert _links_file(data).read_text().startswith("# The links in the viewer's navigation.")


def test_a_write_with_no_row_empties_the_list(client, data) -> None:
    _one_link(data)
    assert _post(client, []).json() == {"ok": True, "links": []}
    assert links_mod.load(data / "wiki") == []


def test_a_bad_url_changes_nothing_and_names_the_row(client, data) -> None:
    _one_link(data)
    before = _links_file(data).read_text()
    response = _post(
        client,
        [
            {"title": "Pantry", "url": PANTRY, "icon": "box"},
            {"title": "Bad", "url": "javascript:alert(1)", "icon": "link"},
        ],
    )
    assert response.status_code == 400
    assert response.json()["error"].startswith("row 2:")
    assert _links_file(data).read_text() == before


def test_an_unknown_icon_changes_nothing(client, data) -> None:
    _one_link(data)
    before = _links_file(data).read_text()
    response = _post(client, [{"title": "Pantry", "url": PANTRY, "icon": "no-such-icon"}])
    assert response.status_code == 400
    assert "no-such-icon" in response.json()["error"]
    assert _links_file(data).read_text() == before


def test_a_body_that_is_not_an_object_is_refused(client, data) -> None:
    response = client.post(
        "/api/v1/links",
        content=b"not json",
        headers={"content-type": "application/json"},
        auth=ALEX,
    )
    assert response.status_code == 400
    assert not _links_file(data).exists()


def test_a_guest_cannot_write(client, data) -> None:
    response = _post(client, [{"title": "Pantry", "url": PANTRY, "icon": "box"}], auth=MIA)
    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}
    assert not _links_file(data).exists()


def test_a_write_needs_the_token_of_the_scope(client, data) -> None:
    rows = [{"title": "Pantry", "url": PANTRY, "icon": "box"}]
    assert _post(client, rows, csrf="").status_code == 403
    assert _post(client, rows, csrf="nope").status_code == 403
    # the token of another scope, and the token of another person, are no good
    other_scope = links_api._csrf_token("alex", "wiki/Home.md")
    assert _post(client, rows, csrf=other_scope).status_code == 403
    assert _post(client, rows, csrf=_token("mia")).status_code == 403
    assert not _links_file(data).exists()


def test_a_write_from_another_origin_is_refused(client, data) -> None:
    response = _post(
        client,
        [{"title": "Pantry", "url": PANTRY, "icon": "box"}],
        headers={"origin": "https://evil.example.test"},
    )
    assert response.status_code == 403
    assert not _links_file(data).exists()


@pytest.mark.skipif(_GIT_MISSING, reason="git binary required")
def test_the_write_commits_to_the_wiki(client, data) -> None:
    wiki = data / "wiki"
    wikigit.ensure_repo(wiki)
    wikigit.commit(wiki, None, "start: sync the wiki")
    assert _post(client, [{"title": "Pantry", "url": PANTRY, "icon": "box"}]).status_code == 200
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True
    ).stdout
    assert "viewer: links .viewer/links.yaml" in log
