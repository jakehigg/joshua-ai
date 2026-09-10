"""Offline tests for taught-skill parsing (``memory/skills.py``).

``is_skill_path``, ``skill_slug``, ``parse_skill``, and the trigger-cleaning
rules are pure — no database, no embedding.
"""

from __future__ import annotations

from joshua_core.memory.skills import (
    EVERYONE,
    KIND_COMMAND,
    KIND_CONVENTION,
    MATCH_PHRASE,
    MATCH_SEMANTIC,
    MAX_TRIGGER_CHARS,
    MAX_TRIGGERS,
    clean_triggers,
    is_skill_path,
    parse_audience,
    parse_skill,
    skill_slug,
    trigger_problem,
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
    assert clean_triggers(["  Movie Time ", "movie time", "", "Party Mode"]) == (
        "movie time",
        "party mode",
    )


def test_clean_triggers_caps_phrase_length() -> None:
    long = "movie " * (MAX_TRIGGER_CHARS // 3)
    (only,) = clean_triggers([long])
    assert len(only) == MAX_TRIGGER_CHARS


def test_clean_triggers_caps_count_and_warns(caplog) -> None:
    phrases = [f"phrase {i}" for i in range(MAX_TRIGGERS + 5)]
    with caplog.at_level("WARNING"):
        cleaned = clean_triggers(phrases, path="wiki/skills/big.md")
    assert len(cleaned) == MAX_TRIGGERS
    assert any("wiki/skills/big.md" in r.getMessage() for r in caplog.records)


# --- trigger validation ------------------------------------------------------


def test_trigger_problem_rejects_a_single_word() -> None:
    # One word fires on every turn that mentions it.
    assert trigger_problem("announce") is not None
    assert trigger_problem("lights") is not None


def test_trigger_problem_rejects_a_fragment() -> None:
    # A phrase that stops at an article is the front half of a sentence. These
    # are the shapes a prompt-to-skill migration produces.
    assert trigger_problem("turn on the") is not None
    assert trigger_problem("turn off the") is not None
    assert trigger_problem("dim the lights and") is not None


def test_trigger_problem_accepts_a_phrase_that_expects_an_object() -> None:
    """A trailing preposition is how a person opens a request.

    The object follows and the person supplies it. Refusing these would refuse
    "add a note for the plumber".
    """
    for template in (
        "add a note for",
        "set the timer to",
        "turn the lamp on",
        "tell the room that",
        "make a list for",
    ):
        assert trigger_problem(template) is None, template


def test_trigger_problem_accepts_a_real_phrase() -> None:
    for good in ("movie time", "turn on the lights", "reset the timer", "what's on tonight"):
        assert trigger_problem(good) is None, good


def test_clean_triggers_drops_the_unusable_and_says_why(caplog) -> None:
    with caplog.at_level("WARNING"):
        kept = clean_triggers(["movie time", "announce", "turn on the"], path="wiki/skills/a.md")
    assert kept == ("movie time",)
    assert "rejected" in caplog.text


# --- audience ----------------------------------------------------------------


def test_audience_defaults_to_everyone() -> None:
    assert parse_audience(None) is EVERYONE
    assert parse_audience("").everyone
    assert parse_audience("everyone").allows("anyone", "guest")


def test_audience_names_one_person() -> None:
    audience = parse_audience("ada")
    assert audience.allows("ada", "member")
    assert not audience.allows("bo", "member")
    # A role does not stand in for a name.
    assert not audience.allows(None, "member")


def test_audience_names_several_people() -> None:
    audience = parse_audience("[ada, bo]")
    assert audience.allows("ada", "guest")
    assert audience.allows("bo", "guest")
    assert not audience.allows("cass", "member")


def test_audience_names_a_role() -> None:
    members = parse_audience("members")
    assert members.allows("ada", "member")
    assert not members.allows("ada", "guest")

    guests = parse_audience("guests")
    assert guests.allows("bo", "guest")
    assert not guests.allows("bo", "member")


def test_audience_mixes_a_role_and_a_name() -> None:
    audience = parse_audience("[members, bo]")
    assert audience.allows("ada", "member")  # by role
    assert audience.allows("bo", "guest")  # by name
    assert not audience.allows("cass", "guest")


def test_audience_that_cannot_be_read_allows_nobody(caplog) -> None:
    # A skill whose audience is unreadable must not fall open.
    with caplog.at_level("WARNING"):
        audience = parse_audience("Someone Else", path="wiki/skills/a.md")
    assert not audience.allows("ada", "member")
    assert not audience.allows(None, "guest")
    assert audience.describe() == "nobody"


# --- frontmatter: kind, for, match -------------------------------------------


def _fm(**kw: str) -> dict[str, str]:
    return {"name": "a skill", **kw}


def test_parse_skill_defaults_to_a_phrase_command_for_everyone() -> None:
    skill = parse_skill(_fm(triggers="[movie time]"), "Dim the lights.", path="wiki/skills/m.md")
    assert skill is not None
    assert skill.kind == KIND_COMMAND
    assert skill.match == MATCH_PHRASE
    assert skill.audience.everyone
    assert skill.fires_from_phrase


def test_parse_skill_reads_kind_for_and_match() -> None:
    skill = parse_skill(
        _fm(triggers="[evening ideas]", kind="command", match="semantic"),
        "Suggest something.",
        path="wiki/skills/d.md",
    )
    assert skill is not None
    assert skill.match == MATCH_SEMANTIC
    assert skill.fires_from_meaning
    assert not skill.fires_from_phrase


def test_convention_page_holds_no_trigger_and_never_fires(caplog) -> None:
    with caplog.at_level("WARNING"):
        skill = parse_skill(
            _fm(kind="convention", triggers="[turn on the]"),
            "Prefer the switch domain.",
            path="wiki/skills/c.md",
        )
    assert skill is not None
    assert skill.kind == KIND_CONVENTION
    assert skill.triggers == ()
    assert not skill.fires_from_phrase
    assert not skill.fires_from_meaning


def test_convention_page_needs_no_triggers_key() -> None:
    skill = parse_skill(_fm(kind="convention"), "Reference text.", path="wiki/skills/c.md")
    assert skill is not None
    assert skill.kind == KIND_CONVENTION


def test_command_page_without_triggers_key_is_not_a_skill() -> None:
    assert parse_skill(_fm(), "Body.", path="wiki/skills/x.md") is None


def test_unknown_kind_or_match_falls_back_to_the_strict_default(caplog) -> None:
    with caplog.at_level("WARNING"):
        skill = parse_skill(
            _fm(triggers="[movie time]", kind="whatever", match="fuzzy"),
            "Body.",
            path="wiki/skills/x.md",
        )
    assert skill is not None
    assert skill.kind == KIND_COMMAND
    assert skill.match == MATCH_PHRASE
