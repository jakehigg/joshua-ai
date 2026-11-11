"""Offline tests for ``GET /api/v1/wiki/tree``: the wiki as nested JSON, from
the one traversal of ``viewer_wiki.build_tree``."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest
from joshua_gateway import viewer, viewer_wiki
from joshua_gateway.viewer_api import wiki as wiki_api
from joshua_shared import config
from starlette.testclient import TestClient

ALEX = ("alex", "alex-secret")

CONFIG = textwrap.dedent("""\
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
    w = data / "wiki"
    _write(w / "Home.md", "# Home\n\nThe front door.\n")
    _write(w / "recipes.md", "---\ntitle: Recipes\n---\n\n# Recipes\n\nWhat we cook.\n")
    _write(w / "recipes/ponzu-sauce.md", "# Ponzu Sauce\n\nCitrus and soy.\n")
    _write(w / "recipes/sauces/aji.md", "# Aji Amarillo\n\nPeruvian.\n")
    _write(w / "loose-page.md", "# Loose\n\nAt the root.\n")
    _write(w / "journal/2026/09/04/2026-09-04.md", "---\ndate: 2026-09-04\n---\n\nA day.\n")
    _write(w / ".obsidian/workspace.md", "hidden\n")


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    data = tmp_path / "data"
    _tree(data)
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(data))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
    return TestClient(viewer.build_app())


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
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
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
