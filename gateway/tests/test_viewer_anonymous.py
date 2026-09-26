"""Offline tests for `viewer.anonymous`: a guest served to a request with no
credentials, so a private network needs no sign-in. Same fixture shape as
`test_viewer.py`."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from joshua_gateway import viewer
from joshua_shared import config
from starlette.testclient import TestClient

ALEX_PW = "alex-secret"
MIA_PW = "mia-secret"
ALEX = ("alex", ALEX_PW)
MIA = ("mia", MIA_PW)

CONFIG_ANON = textwrap.dedent("""\
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
      anonymous: mia
      users:
        alex: ${VIEWER_PW_ALEX:-}
        mia: ${VIEWER_PW_MIA:-}
    """)

CONFIG_NO_ANON = textwrap.dedent("""\
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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(data: Path) -> None:
    _write(data / "wiki/pizza.md", "# Pizza\n\nDough and sauce.\n")
    _write(
        data / "wiki/journal/2026/09/04/oatmeal.md",
        "---\ndate: 2026-09-04\npeople: [alex]\n---\n\nOats.\n",
    )


def _client(monkeypatch, tmp_path, config_text: str) -> TestClient:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    _tree(data)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(config_text)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX_PW)
    monkeypatch.setenv("VIEWER_PW_MIA", MIA_PW)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    return TestClient(viewer.build_app())


@pytest.fixture
def anon_client(monkeypatch, tmp_path) -> TestClient:
    return _client(monkeypatch, tmp_path, CONFIG_ANON)


@pytest.fixture
def no_anon_client(monkeypatch, tmp_path) -> TestClient:
    return _client(monkeypatch, tmp_path, CONFIG_NO_ANON)


# -- reading with no credentials ---------------------------------------------


def test_no_credentials_reads_the_wiki(anon_client):
    response = anon_client.get("/wiki/pizza.md")
    assert response.status_code == 200
    assert "Dough and sauce" in response.text


def test_no_credentials_reads_the_journal(anon_client):
    response = anon_client.get("/journal")
    assert response.status_code == 200
    feed = anon_client.get("/journal/2026/09/04")
    assert feed.status_code == 200
    assert "Oats" in feed.text


def test_no_credentials_gets_the_guest_home_page(anon_client):
    """Whatever the viewer does with the signed-in person works the same for
    the anonymous person: a profile link, and no edit/new links since the
    anonymous person is a guest."""
    home = anon_client.get("/")
    assert home.status_code == 200
    assert "/wiki/people/mia.md" in home.text
    folder = anon_client.get("/wiki/")
    assert 'href="/new' not in folder.text


# -- write routes refuse the anonymous guest, and nothing on disk changes ----


def test_anonymous_guest_cannot_edit(anon_client, tmp_path):
    before = (tmp_path / "data/wiki/pizza.md").read_text()
    assert anon_client.get("/edit/pizza.md").status_code == 403
    token = viewer._csrf_token("mia", "wiki/pizza.md")
    response = anon_client.post(
        "/save", data={"path": "pizza.md", "csrf": token, "body": "changed"}
    )
    assert response.status_code == 403
    assert (tmp_path / "data/wiki/pizza.md").read_text() == before


def test_anonymous_guest_cannot_create(anon_client, tmp_path):
    assert anon_client.get("/new").status_code == 403
    token = viewer._csrf_token("mia", "new:")
    response = anon_client.post(
        "/save", data={"folder": "", "name": "sneaky", "csrf": token, "body": "x"}
    )
    assert response.status_code == 403
    assert not (tmp_path / "data/wiki/sneaky.md").exists()


def test_anonymous_guest_cannot_trash(anon_client, tmp_path):
    path = "wiki/pizza.md"
    token = viewer._csrf_token("mia", path)
    response = anon_client.post("/delete", data={"path": path, "csrf": token})
    assert response.status_code == 403
    original = tmp_path / "data" / "wiki" / "pizza.md"
    assert original.exists()
    assert not (tmp_path / "data" / "wiki" / ".trash").exists()


# -- with anonymous unset, the basic-auth challenge is unchanged -------------


def test_no_credentials_still_gets_401_when_anonymous_is_unset(no_anon_client):
    response = no_anon_client.get("/wiki/pizza.md")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")


# -- a member can still sign in and write on an open viewer ------------------


def test_member_signs_in_and_writes_on_an_open_viewer(anon_client, tmp_path):
    response = anon_client.get("/wiki/pizza.md", auth=ALEX)
    assert response.status_code == 200
    assert 'href="/edit/pizza.md"' in response.text

    token = viewer._csrf_token("alex", "wiki/pizza.md")
    response = anon_client.post(
        "/save",
        data={"path": "pizza.md", "csrf": token, "body": "# Pizza\n\nChanged.\n"},
        auth=ALEX,
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert (tmp_path / "data/wiki/pizza.md").read_text() == "# Pizza\n\nChanged.\n"


def test_wrong_credentials_still_401_even_with_anonymous_set(anon_client):
    """Whoever sends a bad credential is not silently treated as anonymous."""
    response = anon_client.get("/wiki/pizza.md", auth=("alex", "wrong"))
    assert response.status_code == 401


def test_anonymous_person_removed_from_the_roster_falls_back_to_401(monkeypatch, tmp_path):
    """`cfg.viewer.anonymous` can only name a live guest at config load, but a
    request still resolves the person at request time; if config.person()
    ever returns None for that id (the roster changed underneath a cached
    config), auth fails closed instead of granting anything."""
    client = _client(monkeypatch, tmp_path, CONFIG_ANON)
    # Warm the config cache first, so the patch below only affects lookups at
    # request time and never config load's own validation.
    assert client.get("/wiki/pizza.md").status_code == 200
    monkeypatch.setattr(config.JoshuaConfig, "person", lambda self, person_id: None)
    response = client.get("/wiki/pizza.md")
    assert response.status_code == 401
