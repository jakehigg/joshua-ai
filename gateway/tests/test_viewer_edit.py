"""Offline tests for the viewer's editor on a wiki page and a new page. The
journal side of the same editor is in ``test_viewer_journal.py``."""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from joshua_gateway import viewer, viewer_edit
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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(data: Path) -> None:
    w = data / "wiki"
    _write(w / "Home.md", "# Home\n\nThe front door.\n")
    _write(w / "recipes.md", RECIPES)
    _write(w / "recipes/ponzu-sauce.md", "# Ponzu Sauce\n\nCitrus and soy.\n")
    _write(
        w / "journal/2026/09/04/oatmeal.md", "---\ndate: 2026-09-04\npeople: [alex]\n---\n\nOats.\n"
    )


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    data = tmp_path / "data"
    data.mkdir()
    _tree(data)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
    monkeypatch.setenv("VIEWER_PW_MIA", MIA[1])
    return TestClient(viewer.build_app())


def _save(client, rel: str, body: str, auth=ALEX, **extra):
    token = viewer._csrf_token(auth[0], "wiki/" + rel)
    return client.post(
        "/save",
        data={"path": rel, "csrf": token, "body": body, **extra},
        auth=auth,
        follow_redirects=False,
    )


def _new(client, folder: str, name: str, body: str, auth=ALEX):
    token = viewer._csrf_token(auth[0], "new:" + folder)
    return client.post(
        "/save",
        data={"folder": folder, "name": name, "csrf": token, "body": body},
        auth=auth,
        follow_redirects=False,
    )


# -- composing --------------------------------------------------------------


def test_raw_frontmatter_splits_and_rejoins_byte_for_byte():
    front, body = viewer_edit.split_raw_frontmatter(RECIPES)
    assert front == FRONT and body == "# Recipes\n\nWhat we cook.\n"
    assert front + body == RECIPES
    assert viewer_edit.split_raw_frontmatter("---\nno close\n") == ("", "---\nno close\n")
    assert viewer_edit.split_raw_frontmatter("plain\n") == ("", "plain\n")


def test_compose_wiki_keeps_frontmatter_and_normalizes_the_body():
    out = viewer_edit.compose_wiki(RECIPES, "# Recipes\r\n\r\nWhat we cook now.\r\n\r\n")
    assert out == FRONT + "# Recipes\n\nWhat we cook now.\n"


def test_compose_rejects_bad_frontmatter_in_a_new_body():
    with pytest.raises(viewer_edit.EditError):
        viewer_edit.compose_new("---\ntitle: [\n---\n\nx\n")
    assert viewer_edit.compose_new("---\ntitle: ok\n---\n\nx") == "---\ntitle: ok\n---\n\nx\n"


def test_new_page_rel_adds_md_and_refuses_an_escape():
    assert viewer_edit.new_page_rel("recipes", "soup") == "recipes/soup.md"
    assert viewer_edit.new_page_rel("", "sub/soup.md") == "sub/soup.md"
    for bad in ("", "../x", ".hidden", "/abs", "a/./b"):
        with pytest.raises(viewer_edit.EditError):
            viewer_edit.new_page_rel("recipes", bad)


# -- the form ---------------------------------------------------------------


def test_member_sees_edit_and_new_links_and_guest_does_not(client):
    page = client.get("/wiki/recipes/ponzu-sauce.md", auth=ALEX).text
    assert 'href="/edit/recipes/ponzu-sauce.md"' in page
    assert 'href="/edit/' not in client.get("/wiki/recipes/ponzu-sauce.md", auth=MIA).text
    folder = client.get("/wiki/recipes/", auth=ALEX).text
    assert 'href="/new?folder=recipes"' in folder
    assert 'href="/new' not in client.get("/wiki/recipes/", auth=MIA).text
    assert 'href="/new"' in client.get("/wiki/", auth=ALEX).text


def test_edit_form_shows_the_body_and_keeps_the_frontmatter_out_of_it(client):
    html = client.get("/edit/recipes.md", auth=ALEX).text
    assert "What we cook." in html
    assert "frontmatter, kept as it is" in html
    assert "title: Recipes" in html
    assert 'name="people"' not in html  # not a journal entry
    assert "<textarea" in html and "updated:" not in html.split("<textarea")[1]


def test_edit_form_for_a_journal_entry_has_the_people_field(client):
    html = client.get("/edit/journal/2026/09/04/oatmeal.md", auth=ALEX).text
    assert 'name="people"' in html and 'value="alex"' in html


def test_guest_gets_403_on_the_forms_and_the_save(client):
    assert client.get("/edit/recipes.md", auth=MIA).status_code == 403
    assert client.get("/new?folder=recipes", auth=MIA).status_code == 403
    assert _save(client, "recipes.md", "x", auth=MIA).status_code == 403
    assert _new(client, "recipes", "soup", "x", auth=MIA).status_code == 403


def test_forms_need_auth(client):
    assert client.get("/edit/recipes.md").status_code == 401
    assert client.get("/new").status_code == 401
    assert client.post("/save", data={}).status_code == 401


def test_edit_of_a_missing_page_or_a_folder_is_404(client):
    assert client.get("/edit/nope.md", auth=ALEX).status_code == 404
    assert client.get("/edit/recipes", auth=ALEX).status_code == 404
    assert client.get("/new?folder=nope", auth=ALEX).status_code == 404


# -- saving a page ----------------------------------------------------------


def test_member_edits_a_page_and_the_frontmatter_survives(client, tmp_path):
    response = _save(client, "recipes.md", "# Recipes\n\nWhat we cook now.\n")
    assert response.status_code == 303
    assert response.headers["location"] == "/wiki/recipes.md"
    assert (
        tmp_path / "data/wiki/recipes.md"
    ).read_text() == FRONT + "# Recipes\n\nWhat we cook now.\n"


def test_member_edits_a_page_without_frontmatter(client, tmp_path):
    _save(client, "recipes/ponzu-sauce.md", "# Ponzu\r\n\r\nSoy and citrus.")
    assert (
        tmp_path / "data/wiki/recipes/ponzu-sauce.md"
    ).read_text() == "# Ponzu\n\nSoy and citrus.\n"


def test_save_with_a_bad_csrf_or_origin_is_403(client, tmp_path):
    before = (tmp_path / "data/wiki/recipes.md").read_text()
    assert (
        client.post(
            "/save", data={"path": "recipes.md", "csrf": "bad", "body": "x"}, auth=ALEX
        ).status_code
        == 403
    )
    token = viewer._csrf_token("alex", "wiki/recipes.md")
    response = client.post(
        "/save",
        data={"path": "recipes.md", "csrf": token, "body": "x"},
        headers={"Origin": "http://evil.example"},
        auth=ALEX,
    )
    assert response.status_code == 403
    assert (tmp_path / "data/wiki/recipes.md").read_text() == before


def test_save_refuses_a_path_outside_the_wiki_and_a_non_markdown_file(client, tmp_path):
    for rel in ("../people/alex/x.md", "recipes/ponzu-sauce.txt", ".obsidian/x.md"):
        assert _save(client, rel, "x").status_code in (403, 404), rel


def test_save_over_the_size_cap_is_400(client, tmp_path):
    assert _save(client, "recipes.md", "x" * (viewer.MAX_BYTES + 1)).status_code == 400


# -- a new page -------------------------------------------------------------


def test_member_creates_a_page_in_a_folder(client, tmp_path):
    response = _new(client, "recipes", "soup", "# Soup\n\nHot.\n")
    assert response.status_code == 303
    assert response.headers["location"] == "/wiki/recipes/soup.md"
    assert (tmp_path / "data/wiki/recipes/soup.md").read_text() == "# Soup\n\nHot.\n"


def test_a_new_page_can_make_a_folder(client, tmp_path):
    assert _new(client, "", "garden/beds", "# Beds\n").status_code == 303
    assert (tmp_path / "data/wiki/garden/beds.md").read_text() == "# Beds\n"


def test_a_new_page_never_overwrites(client, tmp_path):
    assert _new(client, "", "recipes.md", "x").status_code == 409
    assert (tmp_path / "data/wiki/recipes.md").read_text() == RECIPES


def test_a_new_page_in_the_journal_is_refused(client, tmp_path):
    assert _new(client, "journal/2026/09/04", "sneaky", "x").status_code == 403
    assert _new(client, "", "journal/x", "x").status_code == 403
    assert not (tmp_path / "data/wiki/journal/2026/09/04/sneaky.md").exists()


def test_a_bad_name_shows_the_form_again(client):
    response = _new(client, "recipes", "../escape", "x")
    assert response.status_code == 400
    assert "plain names" in response.text


def test_new_page_csrf_is_bound_to_the_folder(client):
    token = viewer._csrf_token("alex", "new:recipes")
    response = client.post(
        "/save", data={"folder": "garden", "name": "x", "csrf": token, "body": "x"}, auth=ALEX
    )
    assert response.status_code == 403


@pytest.mark.skipif(_GIT_MISSING, reason="git binary required")
def test_edit_and_new_commit_to_the_wiki(client, tmp_path):
    wiki = tmp_path / "data" / "wiki"
    wikigit.ensure_repo(wiki)
    wikigit.commit(wiki, None, "start: sync the wiki")
    _save(client, "recipes.md", "# Recipes\n\nChanged.\n")
    _new(client, "recipes", "soup", "# Soup\n")
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True
    ).stdout
    assert "viewer: edit recipes.md" in log
    assert "viewer: new recipes/soup.md" in log
