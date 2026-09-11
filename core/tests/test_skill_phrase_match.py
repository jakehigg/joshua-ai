"""The near-exact phrase match (``memory/skills.match_trigger``).

These are the acceptance cases for the matcher. A command skill can change the
state of the world, so the negatives matter as much as the positives: each one
below is a turn that must NOT fire a skill, and several of them are turns that
fired one before the match became lexical.

Pure functions, no database and no embedding.
"""

from __future__ import annotations

import pytest
from joshua_core.memory.skills import (
    KIND_COMMAND,
    MATCH_PHRASE,
    MATCH_SEMANTIC,
    Audience,
    Skill,
    match_skills,
    match_trigger,
)

MAX_EXTRA = 2


def fired(turn: str, trigger: str) -> bool:
    return match_trigger(turn, trigger, max_extra=MAX_EXTRA) is not None


# --- the turn must open with the trigger (the anchor rule) -------------------


@pytest.mark.parametrize(
    "turn,trigger",
    [
        # Said exactly.
        ("movie time", "movie time"),
        ("turn on the reading lights", "turn on the reading lights"),
        # An article the trigger does not hold.
        ("turn on reading lights", "turn on the reading lights"),
        # Address and politeness in front.
        ("hey joshua, movie time", "movie time"),
        ("can you turn on the reading lights please", "turn on the reading lights"),
        # One object inserted.
        ("add milk to the list", "add to the list"),
        # Words after the trigger are free: a person may ask for two things.
        ("movie time and tell me the forecast", "movie time"),
        # A trailing word of detail.
        ("what's on tonight then", "what's on tonight"),
        # The curly apostrophe a phone writes.
        ("what’s on tonight", "what's on tonight"),
    ],
)
def test_fires_when_the_person_asked_for_it(turn: str, trigger: str) -> None:
    assert fired(turn, trigger)


@pytest.mark.parametrize(
    "turn,trigger,why",
    [
        # Before the match was lexical, a plain greeting measured 0.9090
        # against a trigger that shared one word with it, and fired a skill
        # that switches a device on.
        ("bright night", "bright day", "shares a word, misses another"),
        # The turn mentions the words but asks something else.
        ("is it a bright day outside?", "bright day", "the words, mid-sentence"),
        ("did we get a bright day?", "bright day", "a question about the past"),
        ("how warm is it today?", "bright day", "no trigger word at all"),
        # Asking ABOUT a behaviour must not run it.
        ("what does the movie time skill do?", "movie time", "a question about the skill"),
        ("remind me what movie time does", "movie time", "a question about the skill"),
        # Reported speech.
        ("i told them to turn on the reading lights", "turn on the reading lights", "reported"),
        # The opposite of the behaviour.
        ("don't turn on the reading lights", "turn on the reading lights", "negated"),
        ("do not turn on the reading lights", "turn on the reading lights", "negated"),
        # Not the trigger at all.
        ("what is the weather?", "bright day", "unrelated"),
        ("the mix tape was fun", "start the mix", "unrelated, shares a word"),
        # A verb of intent is a statement about the speaker, not a request.
        # "I need to wind down after this week" fired a device skill on the
        # instance while "i", "need" and "to" counted as lead filler.
        ("i need to wind down after this week", "wind down", "a statement, not a request"),
        ("i want to wind down", "wind down", "intent, not a request"),
        ("we need to start the mix", "start the mix", "intent, not a request"),
        # The cost of the rule above: this phrasing no longer fires either.
        ("i want to turn on the reading lights", "turn on the reading lights", "intent"),
    ],
)
def test_does_not_fire_when_the_person_did_not_ask(turn: str, trigger: str, why: str) -> None:
    assert not fired(turn, trigger), why


def test_order_matters() -> None:
    assert not fired("lights reading the on turn", "turn on the reading lights")


def test_the_extra_word_budget_is_enforced() -> None:
    # One inserted object is fine.
    assert fired("add milk to the list", "add to the list")
    # A sentence that buries the trigger is not.
    assert not fired(
        "add milk and eggs and bread and cheese to the list",
        "add to the list",
    )


def test_budget_of_zero_demands_the_words_exactly() -> None:
    assert match_trigger("add milk to the list", "add to the list", max_extra=0) is None
    # Stop words stay free even at zero: they carry no request.
    assert (
        match_trigger("turn on the reading lights", "turn on reading lights", max_extra=0)
        is not None
    )


# --- choosing between skills -------------------------------------------------


def _skill(
    path: str,
    triggers: tuple[str, ...],
    *,
    audience: Audience | None = None,
    match: str = MATCH_PHRASE,
) -> Skill:
    return Skill(
        name=path,
        triggers=triggers,
        instructions=f"Do {path}.",
        kind=KIND_COMMAND,
        audience=audience or Audience(),
        match=match,
        path=path,
    )


def test_the_more_specific_trigger_wins() -> None:
    """A fragment must never outrank the phrase it is the front of.

    This is the ``top_k`` starvation case: a general page and a specific page
    both match, and the specific one is what the person asked for.
    """
    general = _skill("general.md", ("turn on the lights",))
    specific = _skill("specific.md", ("turn on the reading lights",))
    hits = match_skills(
        "turn on the reading lights",
        [general, specific],
        person_id="ada",
        role="member",
        max_extra=MAX_EXTRA,
        top_k=1,
    )
    assert [s.path for s, _ in hits] == ["specific.md"]


def test_one_skill_fires_once_even_when_two_triggers_match() -> None:
    skill = _skill("a.md", ("movie time", "movie time now"))
    hits = match_skills(
        "movie time now", [skill], person_id="ada", role="member", max_extra=MAX_EXTRA, top_k=5
    )
    assert len(hits) == 1
    # The longer trigger is the better description of what was said.
    assert hits[0][1].trigger == "movie time now"


def test_a_semantic_skill_never_fires_from_a_phrase() -> None:
    skill = _skill("d.md", ("evening ideas",), match=MATCH_SEMANTIC)
    assert (
        match_skills(
            "evening ideas", [skill], person_id="ada", role="member", max_extra=MAX_EXTRA, top_k=1
        )
        == []
    )


# --- the audience gate -------------------------------------------------------


def test_a_skill_for_one_person_does_not_fire_for_another() -> None:
    """Two people teach the same words and mean different things.

    Without an audience the two are the same string and no matcher can tell
    them apart. This is why ``for`` exists.
    """
    for_ada = _skill(
        "ada.md", ("start my mix",), audience=Audience(everyone=False, people=frozenset({"ada"}))
    )
    for_bo = _skill(
        "bo.md", ("start my mix",), audience=Audience(everyone=False, people=frozenset({"bo"}))
    )
    both = [for_ada, for_bo]

    hits = match_skills(
        "start my mix", both, person_id="ada", role="member", max_extra=MAX_EXTRA, top_k=5
    )
    assert [s.path for s, _ in hits] == ["ada.md"]

    hits = match_skills(
        "start my mix", both, person_id="bo", role="member", max_extra=MAX_EXTRA, top_k=5
    )
    assert [s.path for s, _ in hits] == ["bo.md"]

    # A third person gets neither.
    hits = match_skills(
        "start my mix", both, person_id="cass", role="member", max_extra=MAX_EXTRA, top_k=5
    )
    assert hits == []


def test_a_members_skill_does_not_fire_for_a_guest() -> None:
    skill = _skill(
        "m.md", ("movie time",), audience=Audience(everyone=False, roles=frozenset({"member"}))
    )
    assert match_skills(
        "movie time", [skill], person_id="ada", role="member", max_extra=MAX_EXTRA, top_k=1
    )
    assert (
        match_skills(
            "movie time", [skill], person_id="bo", role="guest", max_extra=MAX_EXTRA, top_k=1
        )
        == []
    )


def test_an_unknown_person_gets_only_the_everyone_skills() -> None:
    shared = _skill("shared.md", ("movie time",))
    private = _skill(
        "p.md", ("movie time",), audience=Audience(everyone=False, people=frozenset({"ada"}))
    )
    hits = match_skills(
        "movie time", [shared, private], person_id=None, role="guest", max_extra=MAX_EXTRA, top_k=5
    )
    assert [s.path for s, _ in hits] == ["shared.md"]
