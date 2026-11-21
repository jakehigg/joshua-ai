"""How each channel writes the link core put in the text.

Core writes one Markdown link per Joshua path
(``path_links.py`` in core, which holds the table of the shapes). This table
carries the Markdown of each shape and what the two platforms make of it:
Telegram keeps the path as the text and sends a link entity beside it,
iMessage keeps the path as the text and puts the address on its own line.
"""

from __future__ import annotations

import pytest
from joshua_channels.adapters.telegram import link_entities
from joshua_channels.links import inline_addresses, split_links

VIEWER = "https://joshua.example.org"

# (what core wrote, the path a person reads, the address it opens)
SHAPES = [
    ("documents/alex/pie.md", f"{VIEWER}/documents/alex/pie"),
    ("documents/shared/plan.html", f"{VIEWER}/documents/shared/plan"),
    ("wiki/recipes/pie.md", f"{VIEWER}/wiki/recipes/pie.md"),
    ("wiki/attachments/2026/08/plant.jpg", f"{VIEWER}/wiki/attachments/2026/08/plant.jpg"),
    (
        "people/alex/attachments/2026/08/bill.pdf",
        f"{VIEWER}/people/alex/attachments/2026/08/bill.pdf",
    ),
    (
        "shared/attachments/team/2026/08/map.png",
        f"{VIEWER}/shared/attachments/team/2026/08/map.png",
    ),
]


@pytest.mark.parametrize(("path", "url"), SHAPES)
def test_telegram_sends_the_path_as_the_text_and_a_link_entity(path: str, url: str) -> None:
    text, links = split_links(f"It is in [{path}]({url}) now.")
    assert text == f"It is in {path} now."
    entities = link_entities(text, links, 0)
    assert [(e.type, e.offset, e.length, e.url) for e in entities] == [
        ("text_link", len("It is in "), len(path), url)
    ]


@pytest.mark.parametrize(("path", "url"), SHAPES)
def test_imessage_puts_the_address_on_its_own_line(path: str, url: str) -> None:
    assert inline_addresses(f"It is in [{path}]({url}) now.") == f"It is in {path}\n{url} now."


def test_a_text_with_no_link_is_unchanged() -> None:
    assert split_links("Nothing to open here.") == ("Nothing to open here.", [])
    assert inline_addresses("Nothing to open here.") == "Nothing to open here."


def test_an_image_is_not_a_link() -> None:
    text = "![a](https://joshua.example.org/wiki/attachments/a.png)"
    assert split_links(text) == (text, [])
    assert inline_addresses(text) == text


def test_telegram_counts_an_entity_in_utf_16_code_units() -> None:
    """Telegram counts the offset of an entity as Python does not: an emoji
    before the link is two units wide."""
    text, links = split_links("🙂 [wiki/a.md](https://joshua.example.org/wiki/a.md)")
    [entity] = link_entities(text, links, 0)
    assert (entity.offset, entity.length) == (3, len("wiki/a.md"))


def test_telegram_links_only_a_part_that_holds_the_whole_link() -> None:
    text, links = split_links("one [wiki/a.md](https://joshua.example.org/wiki/a.md) two")
    assert link_entities(text[:6], links, 0) == []
    assert len(link_entities(text, links, 0)) == 1


def test_telegram_makes_no_entity_for_a_path_with_no_public_address() -> None:
    """Without ``workspaces.viewer_url`` the address is a bare path, which
    Telegram cannot open, so the path is sent as plain text."""
    text, links = split_links("see [wiki/a.md](/wiki/a.md)")
    assert text == "see wiki/a.md"
    assert link_entities(text, links, 0) == []
