"""Offline tests for `viewer.anonymous`: a request with no credentials gets
full access as a synthetic "anonymous" identity, so a private network needs
no sign-in. Same fixture shape as `test_viewer.py`."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from joshua_gateway import viewer
from joshua_shared import config, wikigit
from starlette.testclient import TestClient

_GIT_MISSING = shutil.which("git") is None

ALEX_PW = "alex-secret"
ALEX = ("alex", ALEX_PW)

CONFIG_ANON = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
    viewer:
      enabled: true
      anonymous: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
    """)

CONFIG_NO_ANON = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
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


def test_no_credentials_home_page_has_no_profile_link(anon_client):
    """There is no such person on disk, so the anonymous identity gets no
    profile link and no personal files folder on the home page."""
    home = anon_client.get("/")
    assert home.status_code == 200
    assert "/wiki/people/" not in home.text
    assert "profile" not in home.text
    assert "attachments" not in home.text.lower()


# -- with anonymous:true, every write route works, same result as a member --


def test_no_credentials_sees_edit_and_new_links(anon_client):
    page = anon_client.get("/wiki/pizza.md")
    assert 'href="/edit/pizza.md"' in page.text
    folder = anon_client.get("/wiki/")
    assert 'href="/new"' in folder.text


def test_no_credentials_can_edit(anon_client, tmp_path):
    token = viewer._csrf_token("anonymous", "wiki/pizza.md")
    response = anon_client.post(
        "/save",
        data={"path": "pizza.md", "csrf": token, "body": "# Pizza\n\nChanged by anyone.\n"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert (tmp_path / "data/wiki/pizza.md").read_text() == "# Pizza\n\nChanged by anyone.\n"


def test_no_credentials_can_create(anon_client, tmp_path):
    token = viewer._csrf_token("anonymous", "new:")
    response = anon_client.post(
        "/save",
        data={"folder": "", "name": "soup", "csrf": token, "body": "# Soup\n\nHot.\n"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert (tmp_path / "data/wiki/soup.md").read_text() == "# Soup\n\nHot.\n"


def test_no_credentials_can_trash(anon_client, tmp_path):
    path = "wiki/pizza.md"
    token = viewer._csrf_token("anonymous", path)
    response = anon_client.post(
        "/delete", data={"path": path, "csrf": token}, follow_redirects=False
    )
    assert response.status_code == 303
    original = tmp_path / "data" / "wiki" / "pizza.md"
    assert not original.exists()
    trash = list((tmp_path / "data" / "wiki" / ".trash").rglob("pizza.md"))
    assert len(trash) == 1


def test_anonymous_write_goes_through_the_same_commit_path(anon_client, tmp_path):
    """No separate code path for an anonymous write: the same
    `_commit_write`/`wikigit` call a signed-in member's write makes, so the
    write is attributed `anonymous` in the wiki's git history exactly the
    way any other identity's write is attributed by its id."""
    if _GIT_MISSING:
        pytest.skip("git binary required")
    wiki = tmp_path / "data" / "wiki"
    wikigit.ensure_repo(wiki)
    wikigit.commit(wiki, None, "start: sync the wiki")
    token = viewer._csrf_token("anonymous", "wiki/pizza.md")
    anon_client.post(
        "/save",
        data={"path": "pizza.md", "csrf": token, "body": "# Pizza\n\nChanged.\n"},
        follow_redirects=False,
    )
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True
    ).stdout
    assert "viewer: edit pizza.md" in log


# -- a wrong credential is never silently treated as anonymous --------------


def test_wrong_credentials_still_401_even_with_anonymous_true(anon_client):
    response = anon_client.get("/wiki/pizza.md", auth=("alex", "wrong"))
    assert response.status_code == 401


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


# -- with anonymous unset or false, the basic-auth challenge is unchanged ---


def test_no_credentials_still_gets_401_when_anonymous_is_unset(no_anon_client):
    response = no_anon_client.get("/wiki/pizza.md")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")
