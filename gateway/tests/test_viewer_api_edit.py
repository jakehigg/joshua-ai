"""Offline tests for the JSON routes of the one editor.

``GET /api/v1/edit`` and ``GET /api/v1/new`` give the editor its text and the
CSRF token of its save. ``POST /api/v1/save`` writes a page, an entry, or a
new page, and ``POST /api/v1/delete`` moves a wiki page to the trash. Each
write wants the member role, the token of its scope, and an origin of the
host.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest
from joshua_gateway import viewer
from joshua_gateway.viewer_api import edit as edit_api
from joshua_shared import config, wikigit
from starlette.testclient import TestClient

_GIT_MISSING = shutil.which("git") is None

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

FRONT = '---\ntitle: Recipes\ntags: []\nupdated: "2026-09-03T15:57:47.978Z"\n---\n\n'
RECIPES = FRONT + "# Recipes\n\nWhat we cook.\n"
ENTRY = (
    "---\ndate: 2026-09-04\npeople:\n- alex\nsource: agent\n---\n\n"
    "# Oatmeal for breakfast\n\nAlex had oatmeal.\n"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(data: Path) -> None:
    w = data / "wiki"
    _write(w / "recipes.md", RECIPES)
    _write(w / "recipes/ponzu-sauce.md", "# Ponzu Sauce\n\nCitrus and soy.\n")
    _write(w / "journal/2026/09/04/oatmeal.md", ENTRY)
    _write(data / "people/alex/attachments/2026/09/note.txt", "paid\n")


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    data = tmp_path / "data"
    _tree(data)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
    monkeypatch.setenv("VIEWER_PW_MIA", MIA[1])
    return TestClient(viewer.build_app())


def _editor(client: TestClient, rel: str, auth: tuple[str, str] = ALEX) -> Any:
    return client.get("/api/v1/edit", params={"path": rel}, auth=auth)


def _save(client: TestClient, rel: str, body: str, auth: tuple[str, str] = ALEX, **extra) -> Any:
    fields = {
        "path": rel,
        "csrf": viewer._csrf_token(auth[0], "wiki/" + rel),
        "body": body,
        **extra,
    }
    return client.post("/api/v1/save", json=fields, auth=auth)


def _create(
    client: TestClient, folder: str, name: str, body: str, auth: tuple[str, str] = ALEX
) -> Any:
    fields = {
        "folder": folder,
        "name": name,
        "csrf": viewer._csrf_token(auth[0], "new:" + folder),
        "body": body,
    }
    return client.post("/api/v1/save", json=fields, auth=auth)


def _delete(client: TestClient, path: str, auth: tuple[str, str] = ALEX) -> Any:
    fields = {"path": path, "csrf": viewer._csrf_token(auth[0], path)}
    return client.post("/api/v1/delete", json=fields, auth=auth)


# -- the editor of a page ---------------------------------------------------


def test_the_editor_of_a_page_keeps_the_frontmatter_out_of_the_text(client) -> None:
    response = _editor(client, "recipes.md")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["kind"] == "wiki"
    assert body["rel"] == "recipes.md" and body["title"] == "Recipes"
    assert body["subtitle"] == "recipes.md"
    assert body["body"] == "# Recipes\n\nWhat we cook.\n"
    assert body["kept_frontmatter"] == FRONT
    assert body["people"] is None
    assert body["cancel_url"] == "/wiki/recipes.md"
    assert body["csrf"] == viewer._csrf_token("alex", "wiki/recipes.md")


def test_a_page_with_no_frontmatter_keeps_none(client) -> None:
    body = _editor(client, "recipes/ponzu-sauce.md").json()
    assert body["kept_frontmatter"] is None
    assert body["body"] == "# Ponzu Sauce\n\nCitrus and soy.\n"


def test_the_editor_of_an_entry_carries_the_day_the_source_and_the_people(client) -> None:
    body = _editor(client, "journal/2026/09/04/oatmeal.md").json()
    assert body["kind"] == "journal"
    assert body["title"] == "Oatmeal for breakfast"
    assert body["subtitle"] == "Friday 4 September 2026 · agent · journal/2026/09/04/oatmeal.md"
    assert body["body"] == "Alex had oatmeal."
    assert body["people"] == ["alex"]
    assert body["kept_frontmatter"] is None
    assert body["cancel_url"] == "/journal/2026/09/04"


def test_the_editor_of_a_new_page_names_the_folder_and_the_way_back(client) -> None:
    body = client.get("/api/v1/new", params={"folder": "recipes"}, auth=ALEX).json()
    assert body == {
        "folder": "recipes",
        "cancel_url": "/wiki/recipes/",
        "csrf": viewer._csrf_token("alex", "new:recipes"),
    }
    root = client.get("/api/v1/new", auth=ALEX).json()
    assert root["folder"] == "" and root["cancel_url"] == "/wiki/"


def test_the_editor_of_a_missing_page_a_folder_or_a_missing_folder_is_404(client) -> None:
    assert _editor(client, "nope.md").status_code == 404
    assert _editor(client, "recipes").status_code == 404
    assert _editor(client, "journal/2026/09/04/nope.md").status_code == 404
    assert client.get("/api/v1/new", params={"folder": "nope"}, auth=ALEX).status_code == 404
    assert _editor(client, "nope.md").json() == {"error": "not found"}


# -- the guest, the token, the origin ---------------------------------------


def test_a_guest_gets_403_on_every_write(client, tmp_path) -> None:
    for response in (
        _editor(client, "recipes.md", auth=MIA),
        client.get("/api/v1/new", params={"folder": "recipes"}, auth=MIA),
        _save(client, "recipes.md", "x", auth=MIA),
        _create(client, "recipes", "soup", "x", auth=MIA),
        _delete(client, "wiki/recipes.md", auth=MIA),
    ):
        assert response.status_code == 403
        assert response.json() == {"error": "forbidden"}
    assert (tmp_path / "data/wiki/recipes.md").read_text() == RECIPES


def test_every_write_needs_a_sign_in(client) -> None:
    assert client.get("/api/v1/edit", params={"path": "recipes.md"}).status_code == 401
    assert client.get("/api/v1/new").status_code == 401
    assert client.post("/api/v1/save", json={}).status_code == 401
    assert client.post("/api/v1/delete", json={}).status_code == 401


def test_a_stale_or_missing_token_is_403(client, tmp_path) -> None:
    before = (tmp_path / "data/wiki/recipes.md").read_text()
    for fields in (
        {"path": "recipes.md", "csrf": "stale", "body": "x"},
        {"path": "recipes.md", "body": "x"},
    ):
        response = client.post("/api/v1/save", json=fields, auth=ALEX)
        assert response.status_code == 403
    assert (tmp_path / "data/wiki/recipes.md").read_text() == before


def test_a_token_of_another_scope_is_403(client) -> None:
    """A token is bound to its scope: the folder of a create, the path of an
    edit, and the root-relative path of a delete."""
    fields = {
        "folder": "garden",
        "name": "x",
        "csrf": viewer._csrf_token("alex", "new:recipes"),
        "body": "x",
    }
    assert client.post("/api/v1/save", json=fields, auth=ALEX).status_code == 403
    wrong = {
        "path": "wiki/recipes.md",
        "csrf": viewer._csrf_token("alex", "wiki/recipes/ponzu-sauce.md"),
    }
    assert client.post("/api/v1/delete", json=wrong, auth=ALEX).status_code == 403


def test_a_cross_site_origin_is_403(client, tmp_path) -> None:
    evil = {"Origin": "http://evil.example"}
    save = client.post(
        "/api/v1/save",
        json={
            "path": "recipes.md",
            "csrf": viewer._csrf_token("alex", "wiki/recipes.md"),
            "body": "x",
        },
        headers=evil,
        auth=ALEX,
    )
    assert save.status_code == 403
    remove = client.post(
        "/api/v1/delete",
        json={
            "path": "wiki/recipes.md",
            "csrf": viewer._csrf_token("alex", "wiki/recipes.md"),
        },
        headers=evil,
        auth=ALEX,
    )
    assert remove.status_code == 403
    assert (tmp_path / "data/wiki/recipes.md").read_text() == RECIPES


def test_a_body_that_is_not_a_json_object_is_400(client) -> None:
    response = client.post("/api/v1/save", content=b"not json", auth=ALEX)
    assert response.status_code == 400
    assert response.json() == {"error": edit_api.NOT_JSON}
    assert client.post("/api/v1/delete", json=[1], auth=ALEX).status_code == 400


# -- the save ---------------------------------------------------------------


def test_a_member_edits_a_page_and_the_frontmatter_survives(client, tmp_path) -> None:
    response = _save(client, "recipes.md", "# Recipes\n\nWhat we cook now.\n")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "url": "/wiki/recipes.md"}
    assert (
        tmp_path / "data/wiki/recipes.md"
    ).read_text() == FRONT + "# Recipes\n\nWhat we cook now.\n"


def test_a_member_saves_an_entry_and_goes_to_the_day(client, tmp_path) -> None:
    rel = "journal/2026/09/04/oatmeal.md"
    response = _save(client, rel, "Alex had porridge.\n", people="alex, mia")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "url": "/journal/2026/09/04#oatmeal"}
    assert (tmp_path / "data/wiki" / rel).read_text() == (
        "---\ndate: 2026-09-04\npeople:\n- alex\n- mia\nsource: agent\n---\n\n"
        "# Oatmeal for breakfast\n\nAlex had porridge.\n"
    )


def test_a_member_creates_a_page_in_a_folder(client, tmp_path) -> None:
    response = _create(client, "recipes", "soup", "# Soup\n\nHot.\n")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "url": "/wiki/recipes/soup.md"}
    assert (tmp_path / "data/wiki/recipes/soup.md").read_text() == "# Soup\n\nHot.\n"


def test_a_save_over_the_size_cap_is_400(client, tmp_path) -> None:
    response = _save(client, "recipes.md", "x" * (edit_api.MAX_BYTES + 1))
    assert response.status_code == 400
    assert response.json() == {"error": edit_api.TOO_BIG}
    assert (tmp_path / "data/wiki/recipes.md").read_text() == RECIPES


def test_a_bad_name_says_what_a_name_is(client) -> None:
    response = _create(client, "recipes", "../escape", "x")
    assert response.status_code == 400
    assert "plain names" in response.json()["error"]


def test_a_name_that_is_taken_is_409(client, tmp_path) -> None:
    response = _create(client, "", "recipes.md", "x")
    assert response.status_code == 409
    assert response.json() == {"error": "A page with that name is already there."}
    assert (tmp_path / "data/wiki/recipes.md").read_text() == RECIPES


def test_a_bad_person_id_on_an_entry_is_400(client, tmp_path) -> None:
    rel = "journal/2026/09/04/oatmeal.md"
    response = _save(client, rel, "x", people="Not A Person")
    assert response.status_code == 400
    assert (tmp_path / "data/wiki" / rel).read_text() == ENTRY


def test_a_new_page_in_the_journal_is_refused(client, tmp_path) -> None:
    assert _create(client, "journal/2026/09/04", "sneaky", "x").status_code == 403
    assert _create(client, "", "journal/x", "x").status_code == 403
    assert not (tmp_path / "data/wiki/journal/2026/09/04/sneaky.md").exists()


def test_a_save_outside_the_wiki_is_refused(client) -> None:
    for rel in ("../people/alex/x.md", "recipes/ponzu-sauce.txt", ".obsidian/x.md"):
        assert _save(client, rel, "x").status_code in (403, 404), rel


# -- the delete -------------------------------------------------------------


def test_a_member_moves_a_page_to_the_trash(client, tmp_path) -> None:
    response = _delete(client, "wiki/recipes.md")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "url": "/"}
    assert not (tmp_path / "data/wiki/recipes.md").exists()
    trash = list((tmp_path / "data/wiki/.trash").rglob("recipes.md"))
    assert len(trash) == 1
    assert trash[0].read_text() == RECIPES


def test_a_delete_outside_the_wiki_is_refused(client) -> None:
    """An absolute path escapes every root, and an attachment is read-only."""
    for path in ("/etc/passwd", "attachments/2026/09/note.txt", "wiki/nope.md"):
        assert _delete(client, path).status_code in (403, 404), path


@pytest.mark.skipif(_GIT_MISSING, reason="git binary required")
def test_the_writes_commit_to_the_wiki(client, tmp_path) -> None:
    wiki = tmp_path / "data" / "wiki"
    wikigit.ensure_repo(wiki)
    wikigit.commit(wiki, None, "start: sync the wiki")
    _save(client, "recipes.md", "# Recipes\n\nChanged.\n")
    _create(client, "recipes", "soup", "# Soup\n")
    _delete(client, "wiki/recipes/ponzu-sauce.md")
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True
    ).stdout
    assert "viewer: edit recipes.md" in log
    assert "viewer: new recipes/soup.md" in log
    assert "viewer: trash recipes/ponzu-sauce.md" in log
