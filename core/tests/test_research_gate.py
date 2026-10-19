"""The specificity gate: grounded phrases, the topic name, the shape, and duplicates."""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_core.jobs.research.gate import (
    Candidate,
    Gate,
    cosine,
    grounding_reason,
    names_topic,
    normalize,
    roster_names,
    shape_reason,
    world_reason,
)
from joshua_core.jobs.topics import Topic, topic_names
from joshua_shared import layout
from research_fakes import (
    GENERIC,
    GROUNDED,
    GROUNDED_PHRASES,
    SNAKE,
    WIKI,
    FakeStore,
    word_vector,
)

BUNDLE = f"## {SNAKE}\n\n{WIKI[SNAKE]}"
TERMS = ["Snake plant", "Snakey"]


async def _embed(text: str) -> list[list[float]]:
    return [word_vector(text)]


def _gate(tmp_path: Path, *, existing=(), near=None, embed=_embed) -> Gate:
    return Gate(
        bundle_text=BUNDLE,
        terms=TERMS,
        existing=list(existing),
        store=FakeStore(near),
        dup_sim=0.85,
        data_root=tmp_path,
        embed=embed,
    )


async def test_a_generic_question_is_rejected_for_ungrounded_phrases(tmp_path: Path) -> None:
    verdict = await _gate(tmp_path).check(Candidate(GENERIC, []))
    assert not verdict.accepted
    assert "grounded_in has 0 phrases" in verdict.reason


async def test_a_grounded_question_passes(tmp_path: Path) -> None:
    verdict = await _gate(tmp_path).check(Candidate(GROUNDED, GROUNDED_PHRASES))
    assert verdict.accepted and verdict.reason == ""


async def test_a_phrase_not_in_the_bundle_rejects(tmp_path: Path) -> None:
    phrases = ["gritty soil mix", "bottom watering tray"]
    verdict = await _gate(tmp_path).check(Candidate(GROUNDED, phrases))
    assert "`bottom watering tray` is not in the bundle" in verdict.reason


def test_a_phrase_is_matched_without_case_or_line_breaks() -> None:
    assert grounding_reason(["WATER TO RUNOFF", "water to runoff, then wait"], BUNDLE) == ""
    assert "characters" in grounding_reason(["ab", "gritty soil mix"], BUNDLE)
    assert "characters" in grounding_reason(["x" * 81, "gritty soil mix"], BUNDLE)
    # No stemming: a plural that is not in the bundle does not match.
    assert "not in the bundle" in grounding_reason(["gritty soil mixes", "hall"], BUNDLE)


async def test_a_question_without_the_topic_name_rejects(tmp_path: Path) -> None:
    question = "How often should a plant in a gritty soil mix be watered in winter?"
    verdict = await _gate(tmp_path).check(Candidate(question, GROUNDED_PHRASES))
    assert verdict.reason == "the question does not name the topic"


def test_the_shape_rules() -> None:
    assert "characters" in shape_reason("Snake plant?", TERMS)
    assert "characters" in shape_reason("Snake plant " + "x" * 300 + "?", TERMS)
    assert "question mark" in shape_reason("How often do we water the snake plant.", TERMS)
    assert shape_reason("How often do we water Snakey in winter?", TERMS) == ""
    assert names_topic("is the SNAKE   PLANT ok?", TERMS)
    assert not names_topic("is the snakeplant ok?", TERMS)
    assert not names_topic("anything?", ["", " "])


async def test_a_question_in_the_dossier_is_a_duplicate(tmp_path: Path) -> None:
    gate = _gate(tmp_path, existing=[GROUNDED.upper().rstrip("?")])
    verdict = await gate.check(Candidate(GROUNDED, GROUNDED_PHRASES))
    assert verdict.reason == "the same question is in the dossier"
    assert normalize("A  b?") == "a b"


async def test_a_near_question_of_the_dossier_is_a_duplicate(tmp_path: Path) -> None:
    other = "In winter, should the gritty snake plant be watered to runoff?"
    verdict = await _gate(tmp_path, existing=[other]).check(Candidate(GROUNDED, GROUNDED_PHRASES))
    assert not verdict.accepted and "a question of the dossier is this close" in verdict.reason
    assert verdict.answered_by is None


async def test_a_near_duplicate_of_a_knowledge_page_is_answered_by(tmp_path: Path) -> None:
    page = layout.knowledge_page(SNAKE, "winter-watering", tmp_path)
    page.parent.mkdir(parents=True)
    page.write_text("---\nquestion: How often in winter?\n---\nFour weeks.\n")
    dossier = layout.knowledge_dossier(SNAKE, tmp_path)
    dossier.write_text(f"---\ntopic: {SNAKE}\nquestions: []\n---\n")
    near = {
        "wiki/knowledge/houseplants/snake-plant.md": 0.99,
        "wiki/knowledge/houseplants/snake-plant/winter-watering.md": 0.9,
        "people/alex/x.md": 0.95,
    }
    verdict = await _gate(tmp_path, near=near).check(Candidate(GROUNDED, GROUNDED_PHRASES))
    assert not verdict.accepted
    assert verdict.answered_by == "knowledge/houseplants/snake-plant/winter-watering.md"


async def test_a_far_knowledge_chunk_is_not_a_duplicate(tmp_path: Path) -> None:
    near = {"wiki/knowledge/houseplants/snake-plant/x.md": 0.5}
    verdict = await _gate(tmp_path, near=near).check(Candidate(GROUNDED, GROUNDED_PHRASES))
    assert verdict.accepted


async def test_an_accepted_question_counts_for_the_next(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    assert (await gate.check(Candidate(GROUNDED, GROUNDED_PHRASES))).accepted
    assert not (await gate.check(Candidate(GROUNDED, GROUNDED_PHRASES))).accepted


async def test_a_person_question_skips_the_grounding_and_duplicate_rules(tmp_path: Path) -> None:
    gate = _gate(tmp_path, existing=[GENERIC])
    verdict = await gate.check(Candidate(GENERIC, []), person=True)
    assert verdict.accepted


async def test_with_no_embedding_model_only_the_text_rules_run(tmp_path: Path) -> None:
    async def broken(text: str) -> list[list[float]]:
        raise RuntimeError("no model")

    gate = _gate(
        tmp_path, existing=["In winter, should the gritty snake plant go to runoff?"], embed=broken
    )
    verdict = await gate.check(Candidate(GROUNDED, GROUNDED_PHRASES))
    assert verdict.accepted
    assert gate.notes and "no embedding model" in gate.notes[0]


async def test_an_index_that_fails_does_not_stop_the_gate(tmp_path: Path) -> None:
    class Broken(FakeStore):
        async def kb_near_paths(self, *args, **kwargs):  # type: ignore[override]
            raise RuntimeError("no database")

    gate = Gate(
        bundle_text=BUNDLE,
        terms=TERMS,
        existing=[],
        store=Broken(),
        dup_sim=0.85,
        data_root=tmp_path,
        embed=_embed,
    )
    assert (await gate.check(Candidate(GROUNDED, GROUNDED_PHRASES))).accepted
    assert gate.notes == ["The index check for duplicates did not run."]


def test_cosine() -> None:
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([0, 0], [1, 0]) == 0.0


# -- the topic names and the world rule ---------------------------------------

WINDOW_TOPIC = Topic(
    path="houseplants/snake-plants-living-room-window.md",
    section="houseplants",
    title="Snake Plants (Living Room Window)",
)
WINDOW_TERMS = topic_names(WINDOW_TOPIC)
WINDOW_BUNDLE = (
    "## houseplants/snake-plants-living-room-window.md\n\n"
    "# Snake Plants (Living Room Window)\n\n"
    "Two snake plants in a gritty mix, watered to runoff. The house stays at\n"
    "65-68°F in winter. Fed once on 2026-10-04 at quarter strength.\n"
)
GOOD_WORLD = (
    "How often should a snake plant in a gritty mix, watered to runoff, be watered "
    "from October to March in a 65–68°F house?"
)
GOOD_PHRASES = ["gritty mix", "watered to runoff"]

# The questions of the first live run, with the person and the dates kept
# in their shape. Seven ask about our records; the last asks about the plan.
LIVE_CANDIDATES = [
    "What dose was used on the snake plants on 2026-10-04?",
    "Will Alex verify both snake plants in one pass?",
    "Did we divide the snake plants before the October 4 feeding?",
    "What did the snake plant division in August produce?",
    "Should we move the snake plants away from the window in winter?",
    "When did the snake plants last get water to runoff?",
    "Is our snake plant pot too big after the repot?",
    "Why was the snake plant fed in fall when the plan says spring?",
]


@pytest.mark.parametrize(
    "question",
    ["Is the snake plant cold in winter?", "Are Snake Plants safe for cats?"],
)
def test_a_question_with_a_short_name_of_the_title_names_the_topic(question: str) -> None:
    assert names_topic(question, WINDOW_TERMS)


def test_a_question_about_the_plants_does_not_name_the_topic() -> None:
    assert not names_topic("How much light do the plants need in winter?", WINDOW_TERMS)


def test_the_live_candidates_are_rejected_as_questions_about_us() -> None:
    reasons = [world_reason(q, ["Alex", "alex"]) for q in LIVE_CANDIDATES]
    rejected = [r for r in reasons if r]
    assert len(rejected) >= 6
    assert "names a person" in reasons[1]
    assert "date" in reasons[0] and "date" in reasons[2]
    assert "`what did`" in reasons[3]
    assert "`should we`" in reasons[4]
    assert "`when did`" in reasons[5]
    assert "about us" in reasons[6]


@pytest.mark.parametrize(
    "question",
    [
        GOOD_WORLD,
        "What light level does a snake plant need in the US Midwest in winter?",
        "Does a snake plant in a gritty mix need feeding between October and March?",
    ],
)
def test_a_question_about_the_world_passes_the_world_rule(question: str) -> None:
    assert world_reason(question, ["Alex", "alex"]) == ""


def test_the_date_forms() -> None:
    for question in ("on Oct 4?", "by March 3rd?", "on Sept. 12?", "on 2026-10-04?"):
        assert "date" in world_reason(question)
    assert world_reason("from October to March, at 20 to 30 days?") == ""


async def test_the_gate_rejects_a_person_and_passes_a_world_question(tmp_path: Path) -> None:
    gate = Gate(
        bundle_text=WINDOW_BUNDLE,
        terms=WINDOW_TERMS,
        existing=[],
        store=FakeStore(None),
        dup_sim=0.85,
        data_root=tmp_path,
        embed=_embed,
        people=["Alex", "alex"],
    )
    person = Candidate("Will Alex check the snake plants in one pass?", GOOD_PHRASES)
    assert "names a person" in (await gate.check(person)).reason
    good = await gate.check(Candidate(GOOD_WORLD, GOOD_PHRASES))
    assert good.accepted, good.reason
    asked = await gate.check(Candidate("Should we feed Snake Plants in May?", []), person=True)
    assert asked.accepted


def test_the_roster_names_are_the_names_and_the_ids() -> None:
    from jobs_fakes import make_settings

    assert roster_names(make_settings()) == ["Alex", "alex"]
    assert roster_names(object()) == []
