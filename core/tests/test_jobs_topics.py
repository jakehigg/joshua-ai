"""Topic enumeration over a fixture wiki that holds every case the rules name."""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_core.jobs.topics import (
    Topic,
    is_topic_page,
    list_topics,
    load_topic,
    names_pattern,
    page_title,
    topic_names,
)

EXCLUDE = ["vocabulary.md", "*/README.md"]

TREE = {
    "Home.md": "# Home\n",
    "vocabulary.md": "# Vocabulary\n",
    "notes.md": "No heading here.\n",
    "houseplants.md": "# Houseplants\n\nThe section index.\n",
    "houseplants/snake-plant.md": "---\ntitle: Ignored\n---\n# Snake plant\n\nGritty mix.\n",
    "houseplants/pothos.md": "---\ntitle: Golden pothos\n---\nNo heading.\n",
    "houseplants/README.md": "# Read me\n",
    "houseplants/care/watering.md": "#NoSpace\n## Second level\n",
    "garden/irrigation.md": "# Irrigation\n",
    "journal/2026/10/19/entry.md": "# An entry\n",
    "people/alex.md": "# Alex\n",
    "joshua-docs/memory.md": "# Memory\n",
    "skills/movie-time.md": "# Movie time\n",
    "knowledge/houseplants/snake-plant.md": "# Dossier\n",
    "insights/garden/autumn.md": "# Insight\n",
    "questions/garden/feed.md": "# Question\n",
    "attachments/2026/10/note.md": "# Attached\n",
    ".trash/20261019T000000Z/houseplants/old.md": "# Old\n",
    ".obsidian/workspace.md": "# State\n",
    "houseplants/.drafts/secret.md": "# Draft\n",
    "garden/photo.png": "not markdown",
}


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    for rel, text in TREE.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_list_topics_returns_only_leaf_pages_sorted(wiki: Path) -> None:
    topics = list_topics(wiki, EXCLUDE)
    assert [t.path for t in topics] == [
        "garden/irrigation.md",
        "houseplants/care/watering.md",
        "houseplants/pothos.md",
        "houseplants/snake-plant.md",
        "notes.md",
    ]


def test_topic_section_and_title(wiki: Path) -> None:
    by_path = {t.path: t for t in list_topics(wiki, EXCLUDE)}
    assert by_path["houseplants/snake-plant.md"] == Topic(
        path="houseplants/snake-plant.md", section="houseplants", title="Snake plant"
    )
    # No `# ` heading: the frontmatter title.
    assert by_path["houseplants/pothos.md"].title == "Golden pothos"
    # Neither: the file stem. `#NoSpace` and `## ` are not a first-level heading.
    assert by_path["houseplants/care/watering.md"].title == "watering"
    assert by_path["houseplants/care/watering.md"].section == "houseplants"
    # A root page has no section.
    assert by_path["notes.md"].section == ""
    assert by_path["notes.md"].title == "notes"


@pytest.mark.parametrize(
    "rel",
    [
        "Home.md",
        "vocabulary.md",
        "houseplants.md",
        "houseplants/README.md",
        "journal/2026/10/19/entry.md",
        "people/alex.md",
        "joshua-docs/memory.md",
        "skills/movie-time.md",
        "knowledge/houseplants/snake-plant.md",
        "insights/garden/autumn.md",
        "questions/garden/feed.md",
        "attachments/2026/10/note.md",
        ".trash/20261019T000000Z/houseplants/old.md",
        ".obsidian/workspace.md",
        "houseplants/.drafts/secret.md",
        "garden/photo.png",
        "houseplants/missing.md",
        "../outside.md",
        "/houseplants/snake-plant.md",
        "houseplants\\snake-plant.md",
        "houseplants/./snake-plant.md",
    ],
)
def test_is_topic_page_refuses(wiki: Path, rel: str) -> None:
    assert is_topic_page(rel, wiki, EXCLUDE) is False


def test_is_topic_page_accepts_a_leaf(wiki: Path) -> None:
    assert is_topic_page("houseplants/snake-plant.md", wiki, EXCLUDE) is True
    assert is_topic_page("notes.md", wiki, EXCLUDE) is True


def test_exclude_globs_are_relative_to_the_wiki_root(wiki: Path) -> None:
    assert is_topic_page("houseplants/pothos.md", wiki, ["houseplants/*"]) is False
    assert is_topic_page("vocabulary.md", wiki, []) is True
    # `*` matches a slash, so `*/README.md` covers a deeper README too.
    deep = wiki / "houseplants" / "care" / "README.md"
    deep.write_text("# Deep\n")
    assert is_topic_page("houseplants/care/README.md", wiki, EXCLUDE) is False


def test_list_topics_of_a_missing_wiki(tmp_path: Path) -> None:
    assert list_topics(tmp_path / "nope", EXCLUDE) == []


def test_page_title_rules() -> None:
    assert page_title("# A\n# B\n", "stem") == "A"
    assert page_title("#   \n# Real\n", "stem") == "Real"
    assert page_title("---\ntitle: '  T  '\n---\nbody\n", "stem") == "T"
    assert page_title("---\ntitle: 5\n---\nbody\n", "stem") == "stem"
    assert page_title("", "stem") == "stem"


def test_load_topic_reads_an_unreadable_page_as_its_stem(wiki: Path) -> None:
    (wiki / "garden" / "binary.md").write_bytes(b"\xff\xfe\x00")
    assert load_topic("garden/binary.md", wiki).title == "binary"


# -- names --------------------------------------------------------------------

WINDOW = Topic(
    path="houseplants/snake-plants-living-room-window.md",
    section="houseplants",
    title="Snake Plants (Living Room Window)",
)


def test_the_names_of_a_topic() -> None:
    assert topic_names(WINDOW, ["Sansevieria", "snake plants"]) == [
        "Snake Plants (Living Room Window)",
        "Snake Plants",
        "Sansevieria",
        "snake plants living",
        "snake plants living room",
    ]
    one_word = Topic(path="garden/roses.md", section="garden", title="Roses")
    assert topic_names(one_word) == ["Roses"]


@pytest.mark.parametrize(
    "text",
    [
        "How often should a snake plant be watered?",
        "Do Snake Plants need a gritty mix?",
        "the SNAKE\n  PLANTS by the window",
        "Is the Sansevieria too cold?",
        "snake plants living room window light",
    ],
)
def test_a_name_of_the_topic_is_found(text: str) -> None:
    pattern = names_pattern(topic_names(WINDOW, ["Sansevieria"]))
    assert pattern is not None and pattern.search(text)


@pytest.mark.parametrize(
    "text",
    ["How often should the plants be watered?", "a snakeplant", "snake", "a living room"],
)
def test_a_text_without_a_name_of_the_topic_is_not_found(text: str) -> None:
    pattern = names_pattern(topic_names(WINDOW))
    assert pattern is not None and not pattern.search(text)


def test_a_name_is_matched_with_and_without_its_plural() -> None:
    pattern = names_pattern(["Boxes", "fern"])
    assert pattern is not None
    for text in ("one box", "two boxes", "a fern", "ferns", "FERNES"):
        assert pattern.search(text), text
    moss = names_pattern(["moss"])
    assert moss is not None and moss.search("moss") and not moss.search("mos")


def test_a_short_name_is_not_matched() -> None:
    assert names_pattern(["ab", "", " "]) is None
    assert names_pattern(["ivy"]) is not None
