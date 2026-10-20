"""Topic enumeration over a fixture wiki that holds every case the rules name."""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_core.jobs.topics import (
    Topic,
    drop_people,
    generic_title,
    is_person_page,
    is_topic_page,
    list_topics,
    load_topic,
    names_pattern,
    page_title,
    possessive_forms,
    roster_keys,
    topic_names,
)
from joshua_shared.config import Jobs, Person

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


# -- a person is not a topic --------------------------------------------------

ROSTER = [Person(id="alex", name="Alex"), Person(id="bo", name="Bo")]
PERSON_TREE = {
    "coach/profiles/alex-profile.md": "# Alex\n\nResting heart rate and goals.\n",
    "coach/profiles/bo.md": "# Training notes\n\nBo walks daily.\n",
    "fitness/coach-notes.md": "---\ntitle: alex\n---\nNotes.\n",
    "recipes/caldo-verde.md": "# Alex's caldo verde\n\nKale and potatoes.\n",
    "recipes/alexs-stew.md": "# Alex’s stew\n\nBeans.\n",
    "garden/roses.md": "---\naliases: [Alex, Rosie]\n---\n# Roses\n",
}


@pytest.fixture
def people_wiki(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    for rel, text in PERSON_TREE.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_a_page_titled_with_a_roster_name_is_not_a_topic(people_wiki: Path) -> None:
    paths = [t.path for t in list_topics(people_wiki, EXCLUDE, people=ROSTER)]
    assert paths == ["garden/roses.md", "recipes/alexs-stew.md", "recipes/caldo-verde.md"]
    # The title is a name, the frontmatter title is an id, or the stem is an id.
    for rel in ("coach/profiles/alex-profile.md", "coach/profiles/bo.md", "fitness/coach-notes.md"):
        assert is_topic_page(rel, people_wiki, EXCLUDE, people=ROSTER) is False


def test_with_no_roster_a_person_page_is_a_topic_as_before(people_wiki: Path) -> None:
    assert is_topic_page("coach/profiles/alex-profile.md", people_wiki, EXCLUDE) is True
    assert len(list_topics(people_wiki, EXCLUDE)) == len(PERSON_TREE)
    assert len(list_topics(people_wiki, EXCLUDE, people=[])) == len(PERSON_TREE)


def test_the_default_exclude_holds_the_coach_profiles(people_wiki: Path) -> None:
    exclude = Jobs().topics.exclude
    assert "coach/profiles/*" in exclude
    assert is_topic_page("coach/profiles/alex-profile.md", people_wiki, exclude) is False


def test_is_person_page() -> None:
    assert is_person_page(Topic("coach/alex.md", "coach", "Weekly plan"), ROSTER)
    assert is_person_page(Topic("coach/plan.md", "coach", "  BO "), ROSTER)
    assert not is_person_page(Topic("coach/plan.md", "coach", "Bo's plan"), ROSTER)
    assert not is_person_page(Topic("coach/alex.md", "coach", "Alex"), None)
    assert roster_keys(ROSTER) == {"alex", "bo"}


def test_a_recipe_with_a_name_in_its_title_keeps_no_bare_name() -> None:
    recipe = Topic("recipes/caldo-verde.md", "recipes", "Alex's caldo verde")
    names = topic_names(recipe, people=ROSTER)
    assert names == ["caldo verde"]
    stew = Topic("recipes/alexs-stew.md", "recipes", "Alex’s stew")
    assert topic_names(stew, people=ROSTER) == ["stew"]
    # Without a roster no name is a person's name.
    assert topic_names(recipe) == ["Alex's caldo verde", "caldo verde"]


CALDO = Topic("recipes/alex-s-caldo-verde.md", "recipes", "Alex's Caldo Verde")


def test_a_possessive_title_names_the_thing_and_not_the_owner() -> None:
    assert topic_names(CALDO) == [
        "Alex's Caldo Verde",
        "alex s",
        "alex s caldo",
        "alex s caldo verde",
    ]
    assert topic_names(CALDO, people=ROSTER) == ["Caldo Verde"]


def test_the_caldo_verde_pattern_does_not_match_the_bare_name() -> None:
    pattern = names_pattern(topic_names(CALDO, people=ROSTER))
    assert pattern is not None
    assert pattern.search("Tonight we made caldo verde.")
    assert pattern.search("Alex watered the plants.") is None
    assert pattern.search("Alex's dinner was pizza.") is None


def test_a_stem_phrase_that_starts_with_a_name_is_not_a_name() -> None:
    soup = Topic("recipes/bo-tomato-soup.md", "recipes", "Tomato soup")
    assert topic_names(soup, people=ROSTER) == ["Tomato soup"]
    plural = Topic("recipes/bos-chili.md", "recipes", "Chili")
    assert topic_names(plural, people=ROSTER) == ["Chili"]


@pytest.mark.parametrize(
    ("title", "generic"),
    [
        ("Alex's Caldo Verde", "Caldo Verde"),
        ("alex’s caldo verde", "caldo verde"),
        ("Alexs' soup", "soup"),
        ("Pancakes, Alex's way", "Pancakes"),
        ("Pancakes (Alex's way) for two", "Pancakes for two"),
        ("Chili in Bo's style", "Chili"),
        ("Bo's and Alex's chili", "chili"),
        ("Alex's", ""),
        ("Caldo Verde", "Caldo Verde"),
        ("Alexander's soup", "Alexander's soup"),
    ],
)
def test_generic_title(title: str, generic: str) -> None:
    assert generic_title(title, ROSTER) == generic


def test_generic_title_without_a_roster_is_the_title() -> None:
    assert generic_title("Alex's  Caldo Verde", None) == "Alex's Caldo Verde"


def test_a_name_that_ends_in_s_takes_an_apostrophe_alone() -> None:
    roster = [Person(id="alexis", name="Alexis")]
    assert generic_title("Alexis' bread", roster) == "bread"
    assert generic_title("Alexis's bread", roster) == "bread"


def test_possessive_forms() -> None:
    assert possessive_forms("Alex's Caldo Verde", ROSTER) == ["Alex's Caldo Verde"]
    assert possessive_forms("Alex's Caldo Verde (winter)", ROSTER) == [
        "Alex's Caldo Verde (winter)",
        "Alex's Caldo Verde",
    ]
    assert possessive_forms("Pancakes, Alex's way", ROSTER) == ["Pancakes, Alex's way"]
    assert possessive_forms("Caldo Verde", ROSTER) == []
    assert possessive_forms("Alex's Caldo Verde", None) == []


def test_drop_people_drops_a_name_with_s_and_a_name_that_starts_with_one() -> None:
    names = ["alexs", "alex s", "Alex's", "Bo’s", "alex s caldo", "alexs stew", "Alex garden"]
    assert drop_people(names, ROSTER) == ["Alex garden"]
    assert drop_people(["Pancakes, Alex's way", "Pancakes"], ROSTER) == ["Pancakes"]


def test_a_last_word_that_is_only_the_plural_end_is_kept() -> None:
    pattern = names_pattern(["alex s"])
    assert pattern is not None
    assert pattern.search("Alex watered the plants.") is None
    assert pattern.search("alex s")


def test_an_alias_that_is_a_roster_name_is_not_a_name() -> None:
    roses = Topic("garden/roses.md", "garden", "Roses")
    assert topic_names(roses, ["Alex", "Rosie", "bo"], people=ROSTER) == ["Roses", "Rosie"]
    assert "Alex" in topic_names(roses, ["Alex"])
    pattern = names_pattern(topic_names(roses, ["Alex"], people=ROSTER))
    assert pattern is not None and pattern.search("Alex went for a run.") is None


def test_drop_people_without_a_roster_keeps_every_name() -> None:
    assert drop_people(["Alex", " Alex ", "Roses"], None) == ["Alex", "Roses"]


def test_a_display_name_of_two_words_is_matched_whole() -> None:
    roster = [Person(id="ax", name="Alex Smith")]
    assert drop_people(["Alex Smiths garden", "Alex garden", "pond"], roster) == [
        "Alex garden",
        "pond",
    ]
    assert generic_title("Alex  Smith's pie", roster) == "pie"
