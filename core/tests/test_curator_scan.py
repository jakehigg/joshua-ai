"""The curator scan: the tree, the link graph, the checks, the section map,
and the similar pairs. Pure Python on a temporary wiki."""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_core.jobs.curator import scan
from joshua_core.jobs.curator.scan import (
    load_tree,
    parse_links,
    resolve_link,
    scan_tree,
    section_map,
    similar_pairs,
    target_for,
)


def _wiki(tmp_path: Path, pages: dict[str, str]) -> Path:
    wiki = tmp_path / "data" / "wiki"
    for rel, text in pages.items():
        path = wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return wiki


def _where(tree, source: str, text: str):
    links = parse_links(text)
    assert len(links) == 1, links
    return resolve_link(links[0], source, tree)


@pytest.fixture
def tree(tmp_path: Path):
    wiki = _wiki(
        tmp_path,
        {
            "Home.md": "# Home\n",
            "garden.md": "# Garden\n",
            "garden/roses.md": "# Roses\n",
            "garden/beds/north.md": "# North bed\n",
            "kitchen/bread.md": "# Bread\n",
            "attachments/2026/10/photo.jpg": "x",
            "notes/a/todo.md": "# A\n",
            "notes/b/todo.md": "# B\n",
            "file.txt": "plain\n",
        },
    )
    return load_tree(wiki)


def test_link_forms_resolve(tree) -> None:
    assert _where(tree, "garden.md", "[r](garden/roses.md)").path == "garden/roses.md"
    assert _where(tree, "garden/roses.md", "[n](beds/north)").path == "garden/beds/north.md"
    assert _where(tree, "garden/beds/north.md", "[r](../roses.md)").path == "garden/roses.md"
    assert _where(tree, "kitchen/bread.md", "[r](/wiki/garden/roses.md)").path == (
        "garden/roses.md"
    )
    assert _where(tree, "kitchen/bread.md", "[r](/garden/roses)").path == "garden/roses.md"
    assert _where(tree, "kitchen/bread.md", "[r](<../garden/roses.md#top>)").path == (
        "garden/roses.md"
    )
    assert _where(tree, "Home.md", "[t](file.txt)").path == "file.txt"
    assert _where(tree, "garden.md", "[[garden/roses]]").path == "garden/roses.md"
    assert _where(tree, "kitchen/bread.md", "[[north|the bed]]").path == "garden/beds/north.md"


def test_links_that_are_not_checked(tree) -> None:
    for text in (
        "[x](https://example.com/a.md)",
        "[x](mailto:someone@example.com)",
        "[x](#anchor)",
        "[x](//example.com/a)",
        "[x](../../out.md)",
    ):
        assert _where(tree, "Home.md", text).checked is False, text


def test_broken_and_attachment_links(tree) -> None:
    gone = _where(tree, "garden.md", "[x](garden/tulips.md)")
    assert gone.checked and gone.path is None and gone.wanted == "garden/tulips.md"
    photo = _where(tree, "garden.md", "[p](attachments/2026/10/photo.jpg)")
    assert photo.attachment and photo.path == "attachments/2026/10/photo.jpg"
    missing = _where(tree, "garden.md", "[p](attachments/2026/10/none.jpg)")
    assert missing.attachment and missing.path is None
    assert _where(tree, "Home.md", "[[todo]]").path is None


def test_code_frontmatter_and_images_hold_no_links() -> None:
    text = (
        "---\nsee: '[a](x.md)'\n---\n"
        "Text `[b](y.md)` and ![img](z.png).\n\n```\n[c](w.md)\n```\n\n[d](v.md)\n"
    )
    assert [link.target for link in parse_links(text)] == ["v.md"]


def test_target_for_keeps_the_form(tree) -> None:
    def one(text: str):
        return parse_links(text)[0]

    assert target_for(one("[a](roses)"), "garden/beds/north.md", "kitchen/bread.md") == (
        "../../kitchen/bread"
    )
    assert target_for(one("[a](/wiki/x.md#h)"), "a.md", "kitchen/bread.md") == (
        "/wiki/kitchen/bread.md#h"
    )
    assert target_for(one("[a](/x.md)"), "a.md", "kitchen/bread.md") == "/kitchen/bread.md"
    assert target_for(one("[a](<x y.md>)"), "a.md", "my notes.md") == "<my notes.md>"
    assert target_for(one("[a](x.md)"), "a.md", "my notes.md") == "my%20notes.md"
    assert target_for(one("[[x|label]]"), "a.md", "garden/roses.md") == "garden/roses"


def test_checks_on_a_small_wiki(tmp_path: Path) -> None:
    wiki = _wiki(
        tmp_path,
        {
            "Home.md": (
                "# Home\n\n- [Garden](garden.md)\n- [Gone](gone.md) and [garden](garden.md)\n"
            ),
            "garden.md": "# Garden\n\n- [Roses](garden/roses.md)\n- [Old](garden/old.md)\n",
            "garden/roses.md": "# Roses\n\n[bread](bread.md)\n",
            "garden/lost.md": "# Lost\n",
            "kitchen/bread.md": "# Bread\n",
            "journal/2026/10/01/a.md": "No date here. [x](bread.md)\n",
            "journal/2026/10/02/b.md": "---\ndate: 2026-10-02\n---\nok\n",
            "journal/2026/10/03/c.md": "---\nbad: [\n---\nbroken block\n",
            "journal/2026/10/04/empty.md": "",
            "people/alex.md": "[x](nowhere.md)\n",
            "REPORT.md": "export\n",
            "PRUNE.txt": "x\n",
            ".DS_Store": "x",
            "garden/.DS_Store": "x",
            "people/.DS_Store": "x",
            "blank.md": "  \n\n",
            ".obsidian/workspace.json": "{}",
        },
    )
    result = scan_tree(load_tree(wiki), trash=True)
    found = {(f.kind, f.tier, tuple(f.paths)) for f in result.findings}
    assert ("broken-link", "A", ("garden/roses.md",)) in found
    assert ("broken-link", "C", ("journal/2026/10/01/a.md",)) in found
    assert ("index-line", "A", ("garden.md",)) in found
    # A line with a live link is not dropped; its dead link is Tier C.
    assert ("broken-link", "C", ("Home.md",)) in found
    assert ("orphan", "A", ("garden/lost.md", "garden.md")) in found
    assert ("orphan", "C", ("kitchen/bread.md",)) in found
    assert ("journal-date", "A", ("journal/2026/10/01/a.md",)) in found
    strays = {f.paths[0]: f.tier for f in result.findings if f.kind == "stray"}
    assert strays == {
        ".DS_Store": "A",
        "garden/.DS_Store": "A",
        "people/.DS_Store": "C",
        "REPORT.md": "A",
        "PRUNE.txt": "A",
        "blank.md": "A",
    }
    paths = {p for f in result.findings for p in f.paths}
    assert not {"journal/2026/10/02/b.md", "journal/2026/10/03/c.md"} & paths
    # An empty journal entry is never stray; it only lacks its date.
    assert ("journal-date", "A", ("journal/2026/10/04/empty.md",)) in found
    assert not any(p.startswith(("people/alex", ".obsidian")) for p in paths)
    for f in result.findings:
        assert set(f.detail["hashes"]) <= set(f.paths)


def test_without_home_every_page_is_an_orphan(tmp_path: Path) -> None:
    wiki = _wiki(tmp_path, {"a.md": "# A\n"})
    result = scan_tree(load_tree(wiki), trash=False)
    assert [(f.kind, f.tier) for f in result.findings] == [("orphan", "C")]


def test_a_missing_wiki_is_an_empty_tree(tmp_path: Path) -> None:
    tree = load_tree(tmp_path / "nowhere")
    assert tree.files == frozenset()
    assert scan_tree(tree, trash=True).findings == []


def test_section_map(tmp_path: Path) -> None:
    wiki = _wiki(
        tmp_path,
        {
            "Home.md": (
                "# Home\n\nIntro [x](garden.md)\n\n## Sections\n\n"
                "- [Garden](garden.md): the yard\n"
                "- [Kitchen](/wiki/kitchen.md) — food\n"
                "- [Nowhere](nowhere.md)\n"
                "- plain line\n\n## Other\n\n- [Ignored](garden.md)\n"
            ),
            "garden.md": "---\na: 1\n---\n# Garden\n\nThe yard and\nthe beds.\n\nMore.\n",
            "garden/roses.md": "# Roses\n",
            "kitchen.md": "# Kitchen\n",
        },
    )
    tree = load_tree(wiki)
    sections = section_map(tree)
    assert [(s.name, s.path, s.folder, s.description) for s in sections] == [
        ("Garden", "garden.md", "garden", "the yard"),
        ("Kitchen", "kitchen.md", "", "food"),
    ]
    assert sections[0].summary == "The yard and the beds."
    rendered = scan.render_section_map(sections)
    assert "section: Garden" in rendered and "folder: (wiki root)" in rendered


def test_section_map_without_home(tmp_path: Path) -> None:
    assert section_map(load_tree(_wiki(tmp_path, {"a.md": "x"}))) == []


def test_similar_pairs() -> None:
    vectors = {
        "garden/a.md": [1.0, 0.0, 0.0],
        "garden/b.md": [0.99, 0.1, 0.0],
        "garden/c.md": [0.8, 0.6, 0.0],
        "kitchen/d.md": [0.8, 0.6, 0.0],
        "kitchen/z.md": [0.0, 0.0, 0.0],
    }
    pairs = similar_pairs(vectors, duplicate_sim=0.95, overlap_sim=0.7)
    assert [(p.a, p.b, p.kind) for p in pairs] == [
        ("garden/c.md", "kitchen/d.md", "duplicate"),
        ("garden/a.md", "garden/b.md", "duplicate"),
        ("garden/b.md", "garden/c.md", "overlap"),
        ("garden/a.md", "garden/c.md", "overlap"),
    ]


def test_tree_helpers(tree) -> None:
    assert tree.is_index_page("Home.md")
    assert tree.is_index_page("garden.md")
    assert not tree.is_index_page("garden/roses.md")
    assert tree.index_of_folder("garden") == "garden.md"
    assert tree.index_of_folder("") == "Home.md"
    assert tree.index_of_folder("kitchen") is None
    moved = tree.renamed({"garden/roses.md": "kitchen/roses.md", "file.txt": None})
    assert "kitchen/roses.md" in moved.pages and "file.txt" not in moved.files
    assert tree.by_basename["todo.md"] == ("notes/a/todo.md", "notes/b/todo.md")
    assert scan.journal_day(tree, "journal/2026/10/01/x.md").isoformat() == "2026-10-01"
    assert scan.read_exact(tree.root / "nope.md") == ""


def test_a_link_in_fenced_code_is_not_a_link() -> None:
    text = (
        "# Page\n\n"
        "```text\n[a](a.md)\n```\n"
        "~~~\n[[b]]\n~~~\n"
        "  ````text\n```\n[c](c.md)\n````\n"
        "[d](d.md)\n"
    )
    assert [link.target for link in parse_links(text)] == ["d.md"]


def test_a_shorter_or_other_fence_does_not_close_a_block() -> None:
    text = "````\n```\n~~~~\n[a](a.md)\n```` not a close\n````\n[b](b.md)\n"
    assert [link.target for link in parse_links(text)] == ["b.md"]
