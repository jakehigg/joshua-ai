"""Offline tests for the text of the viewer's editor: how a path decides the
shape of the editor, and what the save composes.

The JSON routes that read and write that text are in
``test_viewer_api_edit.py``, and the journal side of the same editor is in
``test_viewer_journal.py``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from joshua_gateway import viewer, viewer_edit
from joshua_shared import config
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
    plain = viewer_edit.compose_wiki("# Ponzu\n\nCitrus.\n", "# Ponzu\r\n\r\nSoy and citrus.")
    assert plain == "# Ponzu\n\nSoy and citrus.\n"


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


# -- the addresses of the editor --------------------------------------------


def test_member_sees_edit_and_new_links_and_guest_does_not(client):
    """The page and the folder are the React app, which gets the two
    addresses, and the permission, from the JSON route."""
    page = "/api/v1/wiki?path=wiki/recipes/ponzu-sauce.md"
    member = client.get(page, auth=ALEX).json()
    assert member["can_edit"] is True
    assert member["edit_url"] == "/edit/recipes/ponzu-sauce.md"
    assert client.get(page, auth=MIA).json()["can_edit"] is False
    folder = client.get("/api/v1/wiki?path=wiki/recipes", auth=ALEX).json()
    assert folder["can_create"] is True and folder["new_url"] == "/new?folder=recipes"
    assert client.get("/api/v1/wiki?path=wiki/recipes", auth=MIA).json()["can_create"] is False
    assert client.get("/api/v1/wiki?path=wiki", auth=ALEX).json()["new_url"] == "/new"
