"""Offline tests for taught-skill parsing (``memory/skills.py``).

``is_skill_path``, ``skill_slug``, ``parse_skill``, and the trigger-cleaning
rules are pure — no database, no embedding.
"""

from __future__ import annotations

from joshua_core.memory.skills import (
    MAX_TRIGGER_CHARS,
    MAX_TRIGGERS,
    clean_triggers,
    is_skill_path,
    parse_skill,
    skill_slug,
)

# --- is_skill_path / skill_slug ---------------------------------------------


def test_is_skill_path_exact_depth() -> None:
    assert is_skill_path("wiki/skills/movie-time.md")
    assert is_skill_path("wiki/skills/a.md")


def test_is_skill_path_rejects_deeper_or_other_trees() -> None:
    assert not is_skill_path("wiki/skills/sub/x.md")  # deeper: an ordinary page
    assert not is_skill_path("wiki/notes/x.md")
    assert not is_skill_path("journal/skills/x.md")
    assert not is_skill_path("wiki/skills/x.txt")
    assert not is_skill_path("wiki/skills/.md")  # empty slug
    assert not is_skill_path("wiki/skills/UPPER.md")  # slug is lower-case only


def test_skill_slug() -> None:
    assert skill_slug("wiki/skills/movie-time.md") == "movie-time"
    assert skill_slug("wiki/notes/x.md") == ""


# --- parse_skill -------------------------------------------------------------


def test_parse_skill_happy_path() -> None:
    fm = {"name": "movie time", "triggers": '["movie time", "let\'s watch a movie"]'}
    skill = parse_skill(fm, "Dim the lights and turn on the TV.")
    assert skill is not None
    assert skill.name == "movie time"
    assert skill.triggers == ("movie time", "let's watch a movie")
    assert skill.instructions == "Dim the lights and turn on the TV."


def test_parse_skill_name_defaults_empty_when_absent() -> None:
    skill = parse_skill({"triggers": '["hi there"]'}, "Do the thing.")
    assert skill is not None
    assert skill.name == ""  # the indexer falls back to the slug


def test_parse_skill_none_without_frontmatter() -> None:
    assert parse_skill({}, "Some body.") is None


def test_parse_skill_none_with_empty_body() -> None:
    assert parse_skill({"triggers": '["hi"]'}, "   ") is None


def test_parse_skill_none_when_triggers_not_a_list() -> None:
    assert parse_skill({"triggers": "movie time"}, "Body.") is None
    assert parse_skill({"name": "x"}, "Body.") is None  # triggers key absent


def test_parse_skill_empty_list_is_valid_but_off() -> None:
    skill = parse_skill({"triggers": "[]"}, "Body.")
    assert skill is not None
    assert skill.triggers == ()


def test_parse_skill_single_quoted_yaml_list() -> None:
    skill = parse_skill({"triggers": "['party mode', 'start the party']"}, "Body.")
    assert skill is not None
    assert skill.triggers == ("party mode", "start the party")


# --- clean_triggers ----------------------------------------------------------


def test_clean_triggers_strips_lowercases_dedupes() -> None:
    assert clean_triggers(["  Movie Time ", "movie time", "", "Party"]) == (
        "movie time",
        "party",
    )


def test_clean_triggers_caps_phrase_length() -> None:
    long = "x" * (MAX_TRIGGER_CHARS + 50)
    (only,) = clean_triggers([long])
    assert len(only) == MAX_TRIGGER_CHARS


def test_clean_triggers_caps_count_and_warns(caplog) -> None:
    phrases = [f"phrase {i}" for i in range(MAX_TRIGGERS + 5)]
    with caplog.at_level("WARNING"):
        cleaned = clean_triggers(phrases, path="wiki/skills/big.md")
    assert len(cleaned) == MAX_TRIGGERS
    assert any("wiki/skills/big.md" in r.getMessage() for r in caplog.records)
