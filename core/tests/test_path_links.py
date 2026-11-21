"""The path-to-link filter: the shapes it links, and what it leaves alone.

The table covers every shape with the Markdown the filter writes, which is
what the viewer shows. ``channels/tests/test_links.py`` carries the same table
with what Telegram and iMessage make of each one; a core test imports no other
container.
"""

from __future__ import annotations

import pytest
from joshua_core.path_links import find_paths, link_paths, viewer_url

VIEWER = "https://joshua.example.org"


# (the text Joshua writes, the path, the address of the viewer)
SHAPES = [
    ("Your pie is in documents/alex/pie.md now.", "documents/alex/pie.md", "/documents/alex/pie"),
    ("See documents/shared/plan.html.", "documents/shared/plan.html", "/documents/shared/plan"),
    ("I wrote wiki/recipes/pie.md today.", "wiki/recipes/pie.md", "/wiki/recipes/pie.md"),
    (
        "The photo is wiki/attachments/2026/08/plant.jpg.",
        "wiki/attachments/2026/08/plant.jpg",
        "/wiki/attachments/2026/08/plant.jpg",
    ),
    (
        "Your scan is people/alex/attachments/2026/08/bill.pdf.",
        "people/alex/attachments/2026/08/bill.pdf",
        "/people/alex/attachments/2026/08/bill.pdf",
    ),
    (
        "It is in shared/attachments/team/2026/08/map.png.",
        "shared/attachments/team/2026/08/map.png",
        "/shared/attachments/team/2026/08/map.png",
    ),
]


@pytest.mark.parametrize(("text", "path", "route"), SHAPES)
def test_the_viewer_gets_a_markdown_link(text: str, path: str, route: str) -> None:
    assert link_paths(text) == text.replace(path, f"[{path}]({route})")


@pytest.mark.parametrize(("text", "path", "route"), SHAPES)
def test_a_channel_outside_the_viewer_gets_the_public_address(
    text: str, path: str, route: str
) -> None:
    """Telegram and iMessage open an absolute address, not a path."""
    assert link_paths(text, VIEWER) == text.replace(path, f"[{path}]({VIEWER}{route})")


def test_an_absolute_address_uses_the_public_viewer_address() -> None:
    assert viewer_url("wiki/pie.md", "https://joshua.example.org/") == (
        "https://joshua.example.org/wiki/pie.md"
    )


def test_the_filter_links_a_path_that_does_not_exist() -> None:
    """The rule reads nothing, so a path nobody wrote is linked too."""
    assert link_paths("wiki/never-written.md") == "[wiki/never-written.md](/wiki/never-written.md)"


def test_every_path_of_one_text_is_linked() -> None:
    linked = link_paths("wiki/a.md and shared/b.txt")
    assert linked == "[wiki/a.md](/wiki/a.md) and [shared/b.txt](/shared/b.txt)"


def test_not_verified_is_not_special() -> None:
    assert link_paths("Not verified: wiki/a.md holds it.") == (
        "Not verified: [wiki/a.md](/wiki/a.md) holds it."
    )


NEGATIVE = [
    pytest.param("A block:\n```\nwiki/a.md\n```\n", id="fenced block"),
    pytest.param("An open block:\n```\nwiki/a.md\n", id="unclosed fenced block"),
    pytest.param("~~~\nshared/a.txt\n~~~\n", id="tilde fenced block"),
    pytest.param("Read `wiki/a.md` for it.", id="inline code span"),
    pytest.param("Read [wiki/a.md](/wiki/a.md) for it.", id="a Markdown link"),
    pytest.param("![a](wiki/attachments/2026/08/a.png)", id="a Markdown image"),
    pytest.param('<a href="/wiki/a.md">wiki/a.md</a>', id="an HTML anchor"),
    pytest.param("https://example.org/shared/attachments/a.pdf", id="inside a URL"),
    pytest.param(r"C:\Users\name\a.md", id="a Windows path"),
    pytest.param("./src/index.css", id="a relative path"),
    pytest.param("index.css:66", id="a word with a dot"),
    pytest.param("people/ is the folder.", id="a bare people/ with no person"),
    pytest.param("documents/alex/pie.txt", id="a document of another kind"),
    pytest.param("wiki/attachments/2026/08/notes.txt", id="an attachment that is not media"),
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_nothing_is_linked(text: str) -> None:
    assert link_paths(text) == text
    assert find_paths(text) == []


def test_a_path_keeps_the_punctuation_of_the_sentence_out_of_the_link() -> None:
    assert link_paths("It is in shared/notes.txt, there.") == (
        "It is in [shared/notes.txt](/shared/notes.txt), there."
    )
