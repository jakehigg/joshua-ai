"""Offline tests for the viewer's wiki navigation: titles, the folder tree,
the crumbs, the siblings of a page, and the frontmatter pairs.

Every answer here is a plain value. The JSON routes that send it, and the
three shapes they answer, are in ``test_viewer_api_wiki.py``.
"""

from __future__ import annotations

from pathlib import Path

from joshua_gateway import viewer_wiki
from joshua_shared.attachments import AttachmentMeta, write_meta


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


# -- the plain values a page shows -------------------------------------------


def test_meta_pairs_drop_an_empty_value_and_shorten_a_timestamp():
    pairs = viewer_wiki.meta_pairs(
        {"title": "Recipes", "tags": [], "updated": "2026-09-03T15:57:47.978Z"}
    )
    assert pairs == {"title": "Recipes", "updated": "2026-09-03"}
    assert viewer_wiki.meta_pairs({}) == {}


def test_crumbs_name_every_folder_above_a_page(tmp_path):
    _tree(tmp_path / "data")
    wiki = tmp_path / "data" / "wiki"
    assert viewer_wiki.crumbs(wiki, "recipes/sauces", leaf="Aji Amarillo") == [
        {"title": "home", "url": "/"},
        {"title": "wiki", "url": "/wiki/"},
        {"title": "Recipes", "url": "/wiki/recipes/"},  # the index page names it
        {"title": "sauces", "url": "/wiki/recipes/sauces/"},
        {"title": "Aji Amarillo", "url": ""},
    ]


def test_the_last_crumb_of_a_folder_is_the_folder(tmp_path):
    _tree(tmp_path / "data")
    items = viewer_wiki.crumbs(tmp_path / "data" / "wiki", "recipes", leaf=None)
    assert items[-1] == {"title": "Recipes", "url": ""}


def test_siblings_are_the_other_pages_of_the_folder(tmp_path):
    _tree(tmp_path / "data")
    items = viewer_wiki.siblings(tmp_path / "data" / "wiki", "recipes/ponzu-sauce.md")
    titles = [item["title"] for item in items]
    assert "eggy ramen" in titles
    assert "Ponzu Sauce" not in titles  # the page itself is not its own sibling
    assert {"title": "sauces/", "url": "/wiki/recipes/sauces/"} in items
    assert items[0] == {"title": "Recipes", "url": "/wiki/recipes.md"}  # the folder's own page


def test_the_journal_is_no_sibling_of_a_page_at_the_root(tmp_path):
    _tree(tmp_path / "data")
    items = viewer_wiki.siblings(tmp_path / "data" / "wiki", "loose-page.md")
    assert "journal/" not in [item["title"] for item in items]


# ── the files the wiki keeps ─────────────────────────────────────────────────


def test_a_folder_lists_the_files_it_holds(tmp_path: Path) -> None:
    """An attachment is not a page, so it is listed apart from the pages."""
    wiki = tmp_path / "wiki"
    (wiki / "attachments" / "2026" / "09").mkdir(parents=True)
    picture = wiki / "attachments" / "2026" / "09" / "grocery-receipt-d7e122.jpg"
    picture.write_bytes(b"\xff\xd8\xff" * 700)
    write_meta(picture, AttachmentMeta(mime="image/jpeg"))

    folder = viewer_wiki.build_tree(wiki, "attachments/2026/09")

    assert folder is not None
    assert [f.name for f in folder.files] == ["grocery-receipt-d7e122.jpg"]
    assert folder.pages == []
    assert folder.files[0].size == 2100
    # The metadata file is never shown: it is the record of the file, not a file.
    assert "meta.json" not in [f.name for f in folder.files]


def test_a_folder_of_pages_lists_no_files(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    (wiki / "recipes").mkdir(parents=True)
    (wiki / "recipes" / "pizza.md").write_text("# Pizza\n")

    folder = viewer_wiki.build_tree(wiki, "recipes")

    assert folder is not None
    assert folder.files == []
    assert [p.title for p in folder.pages] == ["Pizza"]


def test_a_file_counts_toward_the_folder_count(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    (wiki / "attachments").mkdir(parents=True)
    (wiki / "attachments" / "a.jpg").write_bytes(b"x")

    root = viewer_wiki.build_tree(wiki)

    assert root is not None
    assert root.count() == 1
