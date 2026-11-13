"""Offline tests for `viewer.anonymous`: a request with no cookie gets full
access as a synthetic "anonymous" identity, or as the member that the key
names, so a private network needs no sign-in. Same fixture shape as
`test_viewer.py`."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import httpx
import pytest
from conftest import bad_cookie, signed_in
from joshua_gateway import viewer
from joshua_gateway import viewer_assignments as am
from joshua_shared import config, wikigit
from starlette.testclient import TestClient

_GIT_MISSING = shutil.which("git") is None

ALEX = signed_in("alex")

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

CONFIG_MEMBER = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
      - id: sam
        name: Sam
    viewer:
      enabled: true
      anonymous: alex
      users:
        sam: ${VIEWER_PW_SAM:-}
    """)

SAM = signed_in("sam")

# The wiki is the React app, which reads a page from this route.
PAGE_JSON = "/api/v1/wiki?path=wiki/pizza.md"

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

# A pin instance that is also open: the method says how a person signs in, and
# ``anonymous`` says nobody has to.
CONFIG_PIN_ANON = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
    viewer:
      enabled: true
      anonymous: true
      auth:
        method: pin
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
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    return TestClient(viewer.build_app())


@pytest.fixture
def anon_client(monkeypatch, tmp_path) -> TestClient:
    return _client(monkeypatch, tmp_path, CONFIG_ANON)


@pytest.fixture
def member_client(monkeypatch, tmp_path) -> TestClient:
    _write(tmp_path / "data/people/alex/attachments/2026/09/receipt.txt", "paid\n")
    return _client(monkeypatch, tmp_path, CONFIG_MEMBER)


@pytest.fixture
def no_anon_client(monkeypatch, tmp_path) -> TestClient:
    return _client(monkeypatch, tmp_path, CONFIG_NO_ANON)


@pytest.fixture
def pin_anon_client(monkeypatch, tmp_path) -> TestClient:
    return _client(monkeypatch, tmp_path, CONFIG_PIN_ANON)


# -- reading with no credentials ---------------------------------------------


def test_no_credentials_reads_the_wiki(anon_client):
    response = anon_client.get(PAGE_JSON)
    assert response.status_code == 200
    assert "Dough and sauce" in response.json()["markdown"]


def test_no_credentials_reads_the_journal(anon_client):
    feed = anon_client.get("/api/v1/journal")
    assert feed.status_code == 200
    assert feed.json()["month"] == "2026-09"
    day = anon_client.get("/api/v1/journal/2026/09/04")
    assert day.status_code == 200
    assert "Oats" in day.json()["entries"][0]["markdown"]


def test_no_credentials_home_page_has_no_profile_link(anon_client):
    """There is no such person on disk, so the anonymous identity gets no
    profile link and no personal files folder on its own page."""
    body = anon_client.get("/api/v1/me").json()
    assert body["person"]["id"] == "anonymous"
    assert body["profile_url"] is None
    assert body["attachments"] == []


# -- with anonymous:true, every write route works, same result as a member --


def test_no_credentials_sees_edit_and_new_links(anon_client):
    page = anon_client.get(PAGE_JSON).json()
    assert page["can_edit"] is True and page["edit_url"] == "/edit/pizza.md"
    folder = anon_client.get("/api/v1/wiki?path=wiki").json()
    assert folder["can_create"] is True and folder["new_url"] == "/new"


def test_no_credentials_can_edit(anon_client, tmp_path):
    token = viewer._csrf_token("anonymous", "wiki/pizza.md")
    response = anon_client.post(
        "/api/v1/save",
        json={"path": "pizza.md", "csrf": token, "body": "# Pizza\n\nChanged by anyone.\n"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "url": "/wiki/pizza.md"}
    assert (tmp_path / "data/wiki/pizza.md").read_text() == "# Pizza\n\nChanged by anyone.\n"


def test_no_credentials_can_create(anon_client, tmp_path):
    token = viewer._csrf_token("anonymous", "new:")
    response = anon_client.post(
        "/api/v1/save",
        json={"folder": "", "name": "soup", "csrf": token, "body": "# Soup\n\nHot.\n"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "url": "/wiki/soup.md"}
    assert (tmp_path / "data/wiki/soup.md").read_text() == "# Soup\n\nHot.\n"


def test_no_credentials_can_trash(anon_client, tmp_path):
    path = "wiki/pizza.md"
    token = viewer._csrf_token("anonymous", path)
    response = anon_client.post("/api/v1/delete", json={"path": path, "csrf": token})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "url": "/"}
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
        "/api/v1/save",
        json={"path": "pizza.md", "csrf": token, "body": "# Pizza\n\nChanged.\n"},
    )
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True
    ).stdout
    assert "viewer: edit pizza.md" in log


# -- a cookie that does not stand is never silently treated as anonymous ----


def test_a_bad_cookie_is_still_401_even_with_anonymous_true(anon_client):
    response = anon_client.get(PAGE_JSON, auth=bad_cookie())
    assert response.status_code == 401


# -- a member can still sign in and write on an open viewer ------------------


def test_member_signs_in_and_writes_on_an_open_viewer(anon_client, tmp_path):
    response = anon_client.get(PAGE_JSON, auth=ALEX)
    assert response.status_code == 200
    assert response.json()["edit_url"] == "/edit/pizza.md"

    token = viewer._csrf_token("alex", "wiki/pizza.md")
    response = anon_client.post(
        "/api/v1/save",
        json={"path": "pizza.md", "csrf": token, "body": "# Pizza\n\nChanged.\n"},
        auth=ALEX,
    )
    assert response.status_code == 200
    assert (tmp_path / "data/wiki/pizza.md").read_text() == "# Pizza\n\nChanged.\n"


# -- with anonymous unset or false, a request with no cookie is 401 ---------


def test_no_cookie_still_gets_401_when_anonymous_is_unset(no_anon_client):
    """The 401 is plain JSON and carries no challenge: the sign-in page of the
    app asks, never the browser."""
    response = no_anon_client.get(PAGE_JSON)
    assert response.status_code == 401
    assert "WWW-Authenticate" not in response.headers
    assert response.json() == {"error": "sign in to read this"}


# -- a pin method on an open viewer still needs no sign-in -------------------


def test_pin_with_anonymous_true_serves_with_no_sign_in(pin_anon_client):
    """``anonymous`` wins over the method: a request with no cookie reads the
    wiki and writes it, and the app renders no sign-in page, because
    ``auth-method`` says the viewer is open."""
    page = pin_anon_client.get(PAGE_JSON)
    assert page.status_code == 200
    assert "Dough and sauce" in page.json()["markdown"]
    assert pin_anon_client.get("/api/v1/me").json()["person"]["id"] == "anonymous"
    method = pin_anon_client.get("/api/v1/auth-method")
    assert method.status_code == 200
    assert method.json() == {"method": "pin", "anonymous": True}


# -- with anonymous naming a member, no credentials is that member -----------


def test_readyz_says_open_and_names_no_person(member_client):
    body = member_client.get("/readyz").json()
    assert body["anonymous"] is True
    assert "alex" not in str(body)


def test_no_credentials_home_page_is_the_members_page(member_client):
    body = member_client.get("/api/v1/me").json()
    assert body["person"] == {"id": "alex", "name": "Alex", "role": "member"}
    assert body["profile_url"] == "/wiki/people/alex.md"
    assert {"path": "2026/09/receipt.txt", "url": "/attachments/2026/09/receipt.txt"} in body[
        "attachments"
    ]


def test_no_credentials_reads_the_members_attachments(member_client):
    response = member_client.get("/api/v1/files/attachments/2026/09/receipt.txt")
    assert response.status_code == 200
    assert "paid" in response.text


def test_no_credentials_writes_as_the_member(member_client, tmp_path):
    """The CSRF token is bound to the member, so the write is the member's."""
    wrong = viewer._csrf_token("anonymous", "wiki/pizza.md")
    response = member_client.post(
        "/api/v1/save",
        json={"path": "pizza.md", "csrf": wrong, "body": "# Pizza\n\nNo.\n"},
    )
    assert response.status_code == 403
    token = viewer._csrf_token("alex", "wiki/pizza.md")
    response = member_client.post(
        "/api/v1/save",
        json={"path": "pizza.md", "csrf": token, "body": "# Pizza\n\nAlex.\n"},
    )
    assert response.status_code == 200
    assert (tmp_path / "data/wiki/pizza.md").read_text() == "# Pizza\n\nAlex.\n"


def test_no_credentials_reads_the_members_assignments(member_client, monkeypatch):
    people: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        people.append(request.url.params.get("person"))
        if request.url.path.endswith("/saved"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"items": [], "counts": {}})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        am,
        "from_env",
        lambda env: am.AssignmentsClient("http://assignments", "token", transport=transport),
    )
    assert member_client.get("/api/v1/assignments").status_code == 200
    assert member_client.get("/api/v1/assignments/saved").status_code == 200
    assert people and set(people) == {"alex"}


def test_a_cookie_on_a_member_viewer_is_still_checked(member_client):
    assert member_client.get(PAGE_JSON, auth=bad_cookie()).status_code == 401
    body = member_client.get("/api/v1/me", auth=SAM).json()
    assert body["profile_url"] == "/wiki/people/sam.md"
