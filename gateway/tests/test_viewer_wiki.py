"""Offline tests for the viewer's wiki navigation: titles, the tree on the
home page, folder pages, crumbs, and the sibling list on a page."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from joshua_gateway import viewer, viewer_wiki
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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(data: Path) -> None:
    w = data / "wiki"
    _write(w / "Home.md", "# Home\n\nThe front door.\n")
    _write(
        w / "recipes.md",
        "---\ntitle: Recipes\ntags: []\nsource: wikijs\n"
        'updated: "2026-09-03T15:57:47.978Z"\n---\n\n# Recipes\n\nWhat we cook.\n',
    )
    _write(w / "recipes/ponzu-sauce.md", "# Ponzu Sauce\n\nCitrus and soy.\n")
    _write(w / "recipes/eggy-ramen.md", "Instant ramen, upgraded.\n")
    _write(w / "recipes/sauces/aji.md", "# Aji Amarillo\n\nPeruvian.\n")
    _write(w / "garden/beds.md", "# Beds\n\nSix of them.\n")
    _write(w / "loose-page.md", "# Loose\n\nAt the root.\n")
    _write(w / "people/alex.md", "# Alex\n\nA member.\n")
    _write(w / "journal/2026/09/04/2026-09-04.md", "---\ndate: 2026-09-04\n---\n\nA day.\n")
    _write(w / "joshua-docs/memory.md", "# Memory\n\nShipped.\n")
    _write(w / ".obsidian/workspace.md", "hidden\n")


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


# -- the model --------------------------------------------------------------


def test_page_title_prefers_frontmatter_then_heading_then_fallback():
    assert viewer_wiki.page_title("---\ntitle: From Front\n---\n\n# Heading\n", "x") == "From Front"
    assert viewer_wiki.page_title("# Heading\n\ntext\n", "x") == "Heading"
    assert viewer_wiki.page_title("text only\n", "eggy ramen") == "eggy ramen"


def test_tree_pairs_an_index_page_with_its_folder(tmp_path):
    _tree(tmp_path / "data")
    root = viewer_wiki.build_tree(tmp_path / "data" / "wiki")
    assert root is not None
    assert root.index is not None and root.index.rel == "Home.md"
    names = {f.name: f for f in root.folders}
    assert names["recipes"].index is not None and names["recipes"].title == "Recipes"
    assert names["recipes"].index.rel == "recipes.md"
    assert [p.rel for p in root.pages] == ["loose-page.md"]  # recipes.md is not loose
    assert names["garden"].index is None and names["garden"].title == "garden"
    assert [p.title for p in names["recipes"].pages] == ["eggy ramen", "Ponzu Sauce"]
    assert names["recipes"].folders[0].rel == "recipes/sauces"
    assert names["recipes"].count() == 3
    assert ".obsidian" not in names


def test_subtree_at_a_folder(tmp_path):
    _tree(tmp_path / "data")
    folder = viewer_wiki.build_tree(tmp_path / "data" / "wiki", "recipes")
    assert folder is not None and folder.title == "Recipes" and folder.url == "/wiki/recipes/"
    assert viewer_wiki.build_tree(tmp_path / "data" / "wiki", "nope") is None
    assert viewer_wiki.build_tree(tmp_path / "data" / "wiki", ".obsidian") is None


def test_meta_line_drops_empty_values_and_shortens_a_timestamp():
    html = viewer_wiki.meta_html(
        {"title": "Recipes", "tags": [], "updated": "2026-09-03T15:57:47.978Z"}
    )
    assert "tags" not in html
    assert "updated:</span> 2026-09-03" in html
    assert viewer_wiki.meta_html({}) == ""


# -- home -------------------------------------------------------------------


def test_home_shows_a_tree_with_titles_and_one_journal_link(client):
    html = client.get("/", auth=ALEX).text
    assert '<a href="/wiki/recipes/">Recipes</a>' in html
    assert '<a href="/wiki/recipes/ponzu-sauce.md">Ponzu Sauce</a>' in html
    assert 'href="/journal"' in html
    assert "2026-09-04.md" not in html  # the journal is a feed, not a tree
    assert "other pages" in html and "Loose" in html
    assert "workspace" not in html


def test_shipped_docs_start_closed_and_a_small_folder_open(client):
    html = client.get("/", auth=ALEX).text
    assert '<details open><summary><a href="/wiki/recipes/">' in html
    assert '<details><summary><a href="/wiki/joshua-docs/">' in html


# -- folder pages -----------------------------------------------------------


def test_wiki_root_is_the_front_page_and_the_folders(client):
    html = client.get("/wiki/", auth=ALEX).text
    assert "<h1>Home</h1>" in html
    assert "The front door." in html
    assert '<a href="/wiki/recipes/">Recipes/</a>' in html
    assert '<a href="/journal">journal</a>' in html


def test_wiki_without_slash_redirects(client):
    response = client.get("/wiki", auth=ALEX, follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/wiki/"


def test_folder_page_renders_its_index_and_lists_its_pages(client):
    html = client.get("/wiki/recipes/", auth=ALEX).text
    assert "<h1>Recipes</h1>" in html
    assert html.count("<h1>") == 1  # the index heading is not repeated
    assert "What we cook." in html
    assert '<a href="/wiki/recipes/sauces/">sauces/</a>' in html
    assert '<a href="/wiki/recipes/ponzu-sauce.md">Ponzu Sauce</a>' in html
    assert "› <span>Recipes</span>" in html


def test_a_directory_path_without_slash_redirects_to_the_folder_page(client):
    response = client.get("/wiki/recipes", auth=ALEX, follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/wiki/recipes/"


def test_folder_without_an_index_page_still_lists(client):
    html = client.get("/wiki/garden/", auth=ALEX).text
    assert "<h1>garden</h1>" in html and "Beds" in html


def test_missing_and_hidden_folders_are_404(client):
    assert client.get("/wiki/nope/", auth=ALEX).status_code == 404
    assert client.get("/wiki/.obsidian/", auth=ALEX).status_code == 404


def test_guest_reads_a_folder_page(client):
    assert client.get("/wiki/recipes/", auth=MIA).status_code == 200


# -- a page -----------------------------------------------------------------


def test_a_page_has_crumbs_a_title_and_its_siblings(client):
    html = client.get("/wiki/recipes/sauces/aji.md", auth=ALEX).text
    assert "<title>Aji Amarillo</title>" in html
    assert (
        '<a href="/">home</a> › <a href="/wiki/">wiki</a> › '
        '<a href="/wiki/recipes/">Recipes</a> › <a href="/wiki/recipes/sauces/">sauces</a> › '
        "<span>Aji Amarillo</span>"
    ) in html
    html = client.get("/wiki/recipes/ponzu-sauce.md", auth=ALEX).text
    assert "more in Recipes" in html
    assert '<a href="/wiki/recipes/eggy-ramen.md">eggy ramen</a>' in html
    assert 'href="/wiki/recipes/ponzu-sauce.md"' not in html.split("more in Recipes")[1]
    assert '<a href="/wiki/recipes.md">Recipes</a>' in html  # the folder's own page


def test_frontmatter_is_one_line_not_a_box(client):
    html = client.get("/wiki/recipes.md", auth=ALEX).text
    assert 'class="frontmatter meta"' in html
    assert "wikijs" in html and "2026-09-03" in html
    assert "tags:" not in html


def test_search_shows_titles(client):
    html = client.get("/search", params={"q": "Citrus"}, auth=ALEX).text
    assert ">Ponzu Sauce</a>" in html
