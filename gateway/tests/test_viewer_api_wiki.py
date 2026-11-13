"""Offline tests for the JSON routes of the wiki.

``GET /api/v1/wiki?path=`` answers one of three shapes, by what the path
names: a page, a folder of the wiki, or a file. ``GET /api/v1/wiki/tree``
answers the whole wiki as nested JSON, from the one traversal of
``viewer_wiki.build_tree``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from conftest import ViewerSession, signed_in
from joshua_gateway import viewer, viewer_wiki
from joshua_gateway.viewer_api import wiki as wiki_api
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

# A one-pixel PNG, for the file shape.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000050001a5f645400000000049454e44ae426082"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(data: Path) -> None:
    w = data / "wiki"
    _write(w / "Home.md", "# Home\n\nThe front door.\n")
    _write(w / "recipes.md", "---\ntitle: Recipes\n---\n\n# Recipes\n\nWhat we cook.\n")
    _write(w / "recipes/ponzu-sauce.md", "# Ponzu Sauce\n\nCitrus and soy.\n")
    _write(w / "recipes/sauces/aji.md", "# Aji Amarillo\n\nPeruvian.\n")
    _write(w / "loose-page.md", "# Loose\n\nAt the root.\n")
    _write(w / "journal/2026/09/04/2026-09-04.md", "---\ndate: 2026-09-04\n---\n\nA day.\n")
    _write(w / ".obsidian/workspace.md", "hidden\n")


def _extras(data: Path) -> None:
    """What the three shapes need beside the tree: a folder with no index
    page, a page with frontmatter, an image, a file that is not a page, and a
    file of the shared root."""
    w = data / "wiki"
    _write(w / "garden/beds.md", "# Beds\n\nSix of them.\n")
    _write(
        w / "dated.md",
        "---\ndate: 2026-08-20\ntags: []\n---\n\n# Dated\n\nA page with frontmatter.\n",
    )
    (w / "attachments" / "2026" / "09").mkdir(parents=True, exist_ok=True)
    (w / "attachments" / "2026" / "09" / "plant.png").write_bytes(PNG)
    (w / "recipes" / "menu.pdf").write_bytes(b"%PDF-1.4\n")
    (data / "shared" / "attachments").mkdir(parents=True, exist_ok=True)
    (data / "shared" / "attachments" / "team.png").write_bytes(PNG)


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    data = tmp_path / "data"
    _tree(data)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    return TestClient(viewer.build_app())


@pytest.fixture
def files(client: TestClient, tmp_path) -> TestClient:
    """The same viewer, over a volume that also holds the files of the shapes."""
    _extras(tmp_path / "data")
    return client


def _get(client: TestClient, path: str, auth: ViewerSession = ALEX) -> Any:
    response = client.get("/api/v1/wiki", params={"path": path}, auth=auth)
    assert response.status_code == 200, response.text
    return response.json()


def _child(node: dict[str, Any], title: str) -> dict[str, Any]:
    found = [item for item in node["children"] if item["title"] == title]
    assert found, f"{title} is not in {[item['title'] for item in node['children']]}"
    return found[0]


def test_the_tree_holds_the_front_page_the_folders_and_the_loose_pages(client) -> None:
    root = client.get("/api/v1/wiki/tree", auth=ALEX).json()
    assert root["rel"] == "" and root["url"] == "/wiki/"
    assert root["children"][0] == {
        "title": "Home",
        "rel": "Home.md",
        "url": "/wiki/Home.md",
        "count": 0,
        "children": [],
    }
    recipes = _child(root, "Recipes")
    assert recipes["url"] == "/wiki/recipes/"
    assert _child(recipes, "Ponzu Sauce")["url"] == "/wiki/recipes/ponzu-sauce.md"
    assert _child(recipes, "sauces")["children"][0]["title"] == "Aji Amarillo"
    assert _child(root, "Loose")["url"] == "/wiki/loose-page.md"


def test_the_journal_is_one_node_with_no_day(client) -> None:
    root = client.get("/api/v1/wiki/tree", auth=ALEX).json()
    journal = _child(root, "journal")
    assert journal["url"] == "/journal"
    assert journal["children"] == []
    assert journal["count"] == 1


def test_a_folder_counts_the_pages_under_it(client) -> None:
    root = client.get("/api/v1/wiki/tree", auth=ALEX).json()
    recipes = _child(root, "Recipes")
    assert recipes["count"] == 2
    assert _child(recipes, "sauces")["count"] == 1


def test_a_dot_entry_is_not_in_the_tree(client) -> None:
    text = client.get("/api/v1/wiki/tree", auth=ALEX).text
    assert ".obsidian" not in text
    assert "workspace" not in text


def test_no_wiki_folder_gives_an_empty_tree(monkeypatch, tmp_path) -> None:
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(tmp_path / "empty"))
    client = TestClient(viewer.build_app())
    assert client.get("/api/v1/wiki/tree", auth=ALEX).json() == wiki_api.EMPTY_TREE


def test_the_json_comes_from_one_traversal(tmp_path) -> None:
    _tree(tmp_path / "data")
    folder = viewer_wiki.build_tree(tmp_path / "data" / "wiki")
    assert folder is not None
    assert wiki_api.tree_json(folder)["children"][0]["rel"] == "Home.md"


def test_the_tree_is_json_and_needs_a_sign_in(client) -> None:
    response = client.get("/api/v1/wiki/tree", auth=ALEX)
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/v1/wiki/tree").status_code == 401


# -- one address: a page ----------------------------------------------------


def test_a_page_carries_its_title_its_body_and_its_navigation(files) -> None:
    body = _get(files, "wiki/recipes/ponzu-sauce.md")
    assert body["kind"] == "page"
    assert body["root"] == "wiki"
    assert body["rel"] == "wiki/recipes/ponzu-sauce.md"
    assert body["title"] == "Ponzu Sauce"
    assert body["markdown"] == "# Ponzu Sauce\n\nCitrus and soy.\n"
    assert body["crumbs"] == [
        {"title": "home", "url": "/"},
        {"title": "wiki", "url": "/wiki/"},
        {"title": "Recipes", "url": "/wiki/recipes/"},
        {"title": "Ponzu Sauce", "url": ""},
    ]
    assert {"title": "sauces/", "url": "/wiki/recipes/sauces/"} in body["siblings"]
    assert {"title": "Recipes", "url": "/wiki/recipes.md"} in body["siblings"]
    # The page itself is not one of its own siblings.
    assert "/wiki/recipes/ponzu-sauce.md" not in [i["url"] for i in body["siblings"]]


def test_the_frontmatter_leaves_the_body_and_comes_as_pairs(files) -> None:
    body = _get(files, "wiki/dated.md")
    assert body["frontmatter"] == {"date": "2026-08-20"}  # an empty value is left out
    assert "# Dated" in body["markdown"]
    assert "---" not in body["markdown"] and "tags" not in body["markdown"]


def test_a_member_gets_the_editor_and_the_token_of_the_delete(files) -> None:
    body = _get(files, "wiki/recipes/ponzu-sauce.md")
    assert body["can_edit"] is True
    assert body["edit_url"] == "/edit/recipes/ponzu-sauce.md"
    assert body["delete_csrf"] == viewer._csrf_token("alex", "wiki/recipes/ponzu-sauce.md")


def test_an_owner_gets_the_editor_and_creates_a_page(files) -> None:
    """The owner role reads as a member does on the page and on the folder."""
    page = _get(files, "wiki/recipes/ponzu-sauce.md", auth=SAM)
    assert page["can_edit"] is True
    assert page["delete_csrf"] == viewer._csrf_token("sam", "wiki/recipes/ponzu-sauce.md")
    assert _get(files, "wiki/recipes/", auth=SAM)["can_create"] is True


def test_a_guest_gets_no_edit_and_no_token(files) -> None:
    body = _get(files, "wiki/recipes/ponzu-sauce.md", auth=MIA)
    assert body["can_edit"] is False
    assert body["delete_csrf"] == ""


def test_a_page_of_another_root_keeps_its_path_and_gets_no_neighbour(files, tmp_path) -> None:
    _write(tmp_path / "data" / "shared" / "notes.md", "A note.\n")
    body = _get(files, "shared/notes.md")
    assert body["kind"] == "page" and body["root"] == "shared"
    assert body["title"] == "shared/notes.md"
    assert body["crumbs"] == [] and body["siblings"] == []
    assert body["can_edit"] is False and body["edit_url"] == ""


# -- one address: a folder --------------------------------------------------


def test_a_folder_carries_its_index_page_and_what_is_in_it(files) -> None:
    body = _get(files, "wiki/recipes/")
    assert body["kind"] == "folder"
    assert body["rel"] == "recipes"
    assert body["title"] == "Recipes"
    assert body["index_markdown"] == "What we cook."  # the h1 is the folder title
    assert body["folders"] == [{"title": "sauces", "url": "/wiki/recipes/sauces/"}]
    assert body["pages"] == [{"title": "Ponzu Sauce", "url": "/wiki/recipes/ponzu-sauce.md"}]
    assert body["files"] == [
        {"name": "menu.pdf", "url": "/api/v1/files/wiki/recipes/menu.pdf"},
    ]
    assert body["crumbs"][-1] == {"title": "Recipes", "url": ""}
    assert body["can_create"] is True and body["new_url"] == "/new?folder=recipes"


def test_a_folder_with_no_index_page_has_no_markdown(files) -> None:
    body = _get(files, "wiki/garden/")
    assert body["kind"] == "folder"
    assert body["title"] == "garden"
    assert body["index_markdown"] is None
    assert body["pages"] == [{"title": "Beds", "url": "/wiki/garden/beds.md"}]


def test_the_wiki_root_is_a_folder_and_links_the_journal_as_a_feed(files) -> None:
    body = _get(files, "wiki/")
    assert body["kind"] == "folder" and body["rel"] == ""
    assert body["title"] == "Home"
    assert body["index_markdown"] == "The front door."
    assert body["crumbs"] == []
    assert body["folders"][0] == {"title": "journal", "url": "/journal"}
    assert body["new_url"] == "/new"


def test_a_path_with_no_slash_that_names_a_folder_is_the_folder(files) -> None:
    assert _get(files, "wiki/recipes")["kind"] == "folder"


def test_a_guest_reads_a_folder_but_creates_nothing(files) -> None:
    body = _get(files, "wiki/recipes/", auth=MIA)
    assert body["kind"] == "folder" and body["can_create"] is False


# -- one address: a file ----------------------------------------------------


def test_an_image_is_a_file_that_shows_inline(files) -> None:
    body = _get(files, "wiki/attachments/2026/09/plant.png")
    assert body == {
        "kind": "file",
        "rel": "attachments/2026/09/plant.png",
        "name": "plant.png",
        "url": "/api/v1/files/wiki/attachments/2026/09/plant.png",
        "inline": True,
    }


def test_another_file_downloads(files) -> None:
    body = _get(files, "wiki/recipes/menu.pdf")
    assert body["kind"] == "file" and body["inline"] is False
    assert body["url"] == "/api/v1/files/wiki/recipes/menu.pdf"


def test_a_shared_attachment_is_a_file_of_its_own_root(files) -> None:
    body = _get(files, "shared/attachments/team.png")
    assert body["url"] == "/api/v1/files/shared/attachments/team.png"
    assert body["inline"] is True


# -- what the route refuses -------------------------------------------------


def test_a_path_that_escapes_a_root_is_404(files) -> None:
    for path in ("wiki/../../etc/hostname", "/etc/hostname", "wiki/.obsidian/workspace.md", ""):
        response = files.get("/api/v1/wiki", params={"path": path}, auth=ALEX)
        assert response.status_code == 404, path
        assert response.json() == {"error": "not found"}


def test_a_missing_path_and_an_unknown_root_are_404(files) -> None:
    for path in ("wiki/nope.md", "wiki/nope/", "secrets/x.md"):
        assert files.get("/api/v1/wiki", params={"path": path}, auth=ALEX).status_code == 404, path


def test_a_directory_of_another_root_is_404(files) -> None:
    """Only the wiki is a tree the viewer walks; the other roots hold files."""
    assert files.get("/api/v1/wiki", params={"path": "shared/"}, auth=ALEX).status_code == 404


def test_the_page_is_json_and_needs_a_sign_in(files) -> None:
    response = files.get("/api/v1/wiki", params={"path": "wiki/Home.md"}, auth=ALEX)
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    assert files.get("/api/v1/wiki", params={"path": "wiki/Home.md"}).status_code == 401
