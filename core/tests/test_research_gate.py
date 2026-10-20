"""The specificity gate: grounded phrases, the topic name, the shape, and duplicates."""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_core.jobs.research.gate import (
    ABOUT_PERSON,
    ALREADY_ASKED,
    DISMISSED_BEFORE,
    NO_RECORD,
    PERSON,
    PERSON_SECTION,
    RECORD,
    RULE_DATE,
    RULE_OPENING,
    RULE_PERSON,
    RULE_US,
    WEB,
    AskedQuestion,
    Candidate,
    Gate,
    cosine,
    generic_question,
    grounding_reason,
    names_topic,
    normalize,
    roster_names,
    shape_reason,
    world_reason,
    world_rule,
)
from joshua_core.jobs.topics import Topic, generic_title, possessive_forms, topic_names
from joshua_shared import layout
from joshua_shared.config import Person
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
    assert "characters" in grounding_reason(["x" * 161, "gritty soil mix"], BUNDLE)
    # No stemming: a plural that is not in the bundle does not match.
    assert "not in the bundle" in grounding_reason(["gritty soil mixes", "hall"], BUNDLE)


PRODUCT = (
    "Jack's Gritty Mix (Succulent, Cactus and Bonsai Soil) amended with coco coir, "
    "charcoal, and pumice"
)


def test_a_phrase_may_have_160_characters() -> None:
    bundle = f"## {SNAKE}\n\nRepotted in {PRODUCT}. Watered to runoff.\n" + "y" * 200
    assert len(PRODUCT) > 80
    assert grounding_reason([PRODUCT, "Watered to runoff"], bundle) == ""
    assert grounding_reason(["y" * 160, PRODUCT], bundle) == ""
    reason = grounding_reason(["y" * 161, PRODUCT], bundle)
    assert "has 161 characters; a phrase has 3 to 160" in reason


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


async def test_the_gate_sends_a_person_question_to_a_person(tmp_path: Path) -> None:
    gate = _window_gate(tmp_path)
    person = Candidate("Will Alex check the snake plants in one pass?", GOOD_PHRASES)
    verdict = await gate.check(person)
    assert verdict.accepted and verdict.route == PERSON
    assert verdict.route_reason == RULE_PERSON
    good = await gate.check(Candidate(GOOD_WORLD, GOOD_PHRASES))
    assert good.accepted and good.route == WEB, good.reason
    asked = await gate.check(Candidate("Should we feed Snake Plants in May?", []), person=True)
    assert asked.accepted and asked.route == WEB


# -- the route --------------------------------------------------------------------

WINDOW = "houseplants/snake-plants-living-room-window.md"
FEED_ENTRY = "journal/2026/10/04/snake-plants-aloe-watered-fed.md"
# Two questions of the third live run. The roster of the tests has Alex.
RUN3_DOSE = (
    "What product and dose was actually used in the 2026-10-04 fertilizer application "
    "for the Snake Plants, since it wasn't recorded?"
)
RUN3_CHECK = "At the 2026-11-10 check, will Alex verify both Snake Plants in one pass?"
RECORD_Q = "When were the snake plants last given water to runoff in the gritty mix?"


def _window_wiki(tmp_path: Path) -> Path:
    wiki = layout.wiki_root(tmp_path)
    (wiki / WINDOW).parent.mkdir(parents=True, exist_ok=True)
    (wiki / WINDOW).write_text(WINDOW_BUNDLE.split("\n\n", 1)[1])
    (wiki / FEED_ENTRY).parent.mkdir(parents=True, exist_ok=True)
    (wiki / FEED_ENTRY).write_text("---\ndate: 2026-10-04\n---\nWatered and fed.\n")
    return wiki


def _window_gate(tmp_path: Path, *, near=None, asked=(), store=None, **kwargs) -> Gate:
    return Gate(
        bundle_text=WINDOW_BUNDLE,
        terms=WINDOW_TERMS,
        existing=[],
        store=store or FakeStore(near),
        dup_sim=0.85,
        data_root=tmp_path,
        embed=_embed,
        people=["Alex", "alex"],
        record_sim=0.80,
        asked=list(asked),
        **kwargs,
    )


@pytest.mark.parametrize(
    ("question", "audience", "route", "reason"),
    [
        (RUN3_DOSE, "web", PERSON, RULE_DATE),
        (RUN3_CHECK, "web", PERSON, RULE_PERSON),
        (RUN3_CHECK, "record", PERSON, RULE_PERSON),
        (GOOD_WORLD, "web", WEB, ""),
        ("Did we feed the snake plants at quarter strength?", "web", PERSON, RULE_OPENING),
        ("Is our snake plant mix too gritty for winter?", "record", PERSON, RULE_US),
        (GOOD_WORLD, "bogus", WEB, ""),
    ],
)
async def test_the_live_questions_route(
    tmp_path: Path, question: str, audience: str, route: str, reason: str
) -> None:
    _window_wiki(tmp_path)
    verdict = await _window_gate(tmp_path).check(Candidate(question, GOOD_PHRASES, "w", audience))
    assert verdict.accepted, verdict.reason
    assert (verdict.route, verdict.route_reason) == (route, reason)


async def test_a_record_question_with_a_strong_hit_is_answered_by_it(tmp_path: Path) -> None:
    _window_wiki(tmp_path)
    store = FakeStore({f"wiki/{FEED_ENTRY}": 0.91, f"wiki/{WINDOW}": 0.83})
    gate = _window_gate(tmp_path, store=store)
    verdict = await gate.check(Candidate(RECORD_Q, GOOD_PHRASES, "w", "record"))
    assert verdict.accepted and verdict.route == RECORD
    assert verdict.answered_by == FEED_ENTRY
    assert {"kinds": ["wiki", "journal", "knowledge"], "min_sim": 0.80} in store.calls


async def test_a_record_question_with_no_hit_goes_to_a_person(tmp_path: Path) -> None:
    _window_wiki(tmp_path)
    store = FakeStore({f"wiki/{FEED_ENTRY}": 0.79})
    verdict = await _window_gate(tmp_path, store=store).check(
        Candidate(RECORD_Q, GOOD_PHRASES, "", "record")
    )
    assert verdict.accepted and verdict.route == PERSON
    assert verdict.route_reason == NO_RECORD
    assert verdict.answered_by is None


async def test_a_record_is_a_topic_page_an_entry_or_an_answer_page(tmp_path: Path) -> None:
    wiki = _window_wiki(tmp_path)
    pages = {
        "journal/2026/10/05/research-report.md": "---\nsource: research\n---\nReport.\n",
        "joshua-docs/jobs.md": "# Jobs\n",
        "knowledge/houseplants/snake-plants-living-room-window.md": "---\ntopic: x\n---\n",
        "houseplants/.hidden/page.md": "# Hidden\n",
    }
    for rel, text in pages.items():
        (wiki / rel).parent.mkdir(parents=True, exist_ok=True)
        (wiki / rel).write_text(text)
    near = {f"wiki/{rel}": 0.95 for rel in pages}
    near["wiki/houseplants/gone.md"] = 0.97
    near["shared/notes.md"] = 0.99
    verdict = await _window_gate(tmp_path, near=near).check(
        Candidate(RECORD_Q, GOOD_PHRASES, "", "record")
    )
    assert verdict.route == PERSON and verdict.route_reason == NO_RECORD
    near[f"wiki/{WINDOW}"] = 0.81
    again = await _window_gate(tmp_path, near=near).check(
        Candidate(RECORD_Q, GOOD_PHRASES, "", "record")
    )
    assert again.route == RECORD and again.answered_by == WINDOW


async def test_a_record_search_that_fails_goes_to_a_person(tmp_path: Path) -> None:
    class Broken(FakeStore):
        async def kb_near_paths(self, embeddings, *, kinds, min_sim, source="files"):
            if "wiki" in kinds:
                raise RuntimeError("no index")
            return {}

    gate = _window_gate(tmp_path, store=Broken())
    verdict = await gate.check(Candidate(RECORD_Q, GOOD_PHRASES, "", "record"))
    assert verdict.route == PERSON and verdict.route_reason == NO_RECORD
    assert "The record search did not run." in gate.notes


async def test_a_worker_person_question_needs_why(tmp_path: Path) -> None:
    question = "Which liquid fertilizer is in the cupboard for the snake plants?"
    gate = _window_gate(tmp_path)
    bare = await gate.check(Candidate(question, GOOD_PHRASES, " ", "person"))
    assert not bare.accepted and "needs `why`" in bare.reason
    told = await gate.check(Candidate(question, GOOD_PHRASES, "To set the dose.", "person"))
    assert told.accepted and told.route == PERSON
    assert told.route_reason == "the raise worker asks a person"


async def test_the_shape_rules_still_reject_a_person_question(tmp_path: Path) -> None:
    gate = _window_gate(tmp_path)
    ungrounded = await gate.check(Candidate(RUN3_DOSE, [], "w", "person"))
    assert not ungrounded.accepted and "grounded_in" in ungrounded.reason
    nameless = await gate.check(Candidate("What dose went on 2026-10-04?", GOOD_PHRASES))
    assert not nameless.accepted and "name the topic" in nameless.reason
    assert (await gate.check(Candidate(RUN3_DOSE, GOOD_PHRASES))).route == PERSON
    twice = await gate.check(Candidate(RUN3_DOSE, GOOD_PHRASES))
    assert not twice.accepted and twice.route == ""


@pytest.mark.parametrize(
    ("status", "reason"),
    [("open", ALREADY_ASKED), ("answered", ALREADY_ASKED), ("dismissed", DISMISSED_BEFORE)],
)
async def test_a_person_question_asked_before_is_not_asked_again(
    tmp_path: Path, status: str, reason: str
) -> None:
    page = "questions/houseplants/product-dose.md"
    asked = [AskedQuestion(RUN3_DOSE.upper(), status, page)]
    verdict = await _window_gate(tmp_path, asked=asked).check(Candidate(RUN3_DOSE, GOOD_PHRASES))
    assert not verdict.accepted and verdict.route == PERSON
    assert verdict.reason == reason and verdict.page == page


async def test_a_near_person_question_counts_as_asked(tmp_path: Path) -> None:
    near_text = "Which feed went on the snake plants at the watering to runoff in the gritty mix?"
    asked = [AskedQuestion("A gritty mix watered to runoff?", "open", "questions/h/x.md")]
    verdict = await _window_gate(tmp_path, asked=asked).check(
        Candidate(near_text, GOOD_PHRASES, "w", "person")
    )
    assert verdict.reason == ALREADY_ASKED
    far = [AskedQuestion("Which pest showed up on the leaf?", "open", "questions/h/y.md")]
    fresh = await _window_gate(tmp_path, asked=far).check(
        Candidate(near_text, GOOD_PHRASES, "w", "person")
    )
    assert fresh.accepted and fresh.route == PERSON


def test_world_rule_names_each_rule() -> None:
    names = [world_rule(q, ["Alex", "alex"]) for q in LIVE_CANDIDATES]
    assert names[:7] == [
        RULE_DATE,
        RULE_PERSON,
        RULE_DATE,
        RULE_OPENING,
        RULE_OPENING,
        RULE_OPENING,
        RULE_US,
    ]
    assert names[7] == ""


def test_the_roster_names_are_the_names_and_the_ids() -> None:
    from jobs_fakes import make_settings

    assert roster_names(make_settings()) == ["Alex", "alex"]
    assert roster_names(object()) == []


# -- a person section ---------------------------------------------------------


async def test_a_person_section_routes_a_web_question_to_a_person(tmp_path: Path) -> None:
    gate = _window_gate(tmp_path, person_section=True)
    verdict = await gate.check(Candidate(GOOD_WORLD, GOOD_PHRASES, audience=WEB))
    assert (verdict.accepted, verdict.route, verdict.route_reason) == (True, PERSON, PERSON_SECTION)


async def test_a_person_section_question_for_a_person_needs_no_why(tmp_path: Path) -> None:
    gate = _window_gate(tmp_path, person_section=True)
    verdict = await gate.check(Candidate(GOOD_WORLD, GOOD_PHRASES, why="", audience=PERSON))
    assert (verdict.accepted, verdict.route, verdict.route_reason) == (True, PERSON, PERSON_SECTION)


async def test_a_person_section_still_allows_a_record_question(tmp_path: Path) -> None:
    _window_wiki(tmp_path)
    gate = _window_gate(tmp_path, near={f"wiki/{FEED_ENTRY}": 0.95}, person_section=True)
    verdict = await gate.check(Candidate(RECORD_Q, GOOD_PHRASES, audience=RECORD))
    assert (verdict.route, verdict.answered_by) == (RECORD, FEED_ENTRY)


# -- about a person -----------------------------------------------------------

PLAN_TERMS = ["Endurance plan", "endurance plan"]
PLAN_BUNDLE = (
    "## fitness/endurance-plan.md\n\n# Endurance plan\n\n"
    "The plan is the river loop, 3 miles, at dawn. Alex trains on weekdays.\n"
)
PLAN_PHRASES = ["river loop", "3 miles"]
# The five web questions of research run 11 that carried the health of a
# person with no name, reworded for the test roster.
RUN11 = [
    "Which endurance plan pace on the river loop suits an adult with physician-diagnosed "
    "hypertension?",
    "How should weight-loss-focused endurance training fit an endurance plan of 3 miles?",
    "What endurance plan intervals suit a max heart rate of 150 bpm on the river loop?",
    "Does a ~603 kcal breakfast fuel an endurance plan of 3 miles at dawn?",
    "Is 24.5 g of fiber a day enough for an endurance plan on the river loop?",
]
THING = "What shoe cushioning suits an endurance plan of 3 miles on a gravel river loop?"


def _plan_gate(tmp_path: Path) -> Gate:
    return Gate(
        bundle_text=PLAN_BUNDLE,
        terms=PLAN_TERMS,
        existing=[],
        store=FakeStore(),
        dup_sim=0.85,
        data_root=tmp_path,
        embed=_embed,
        people=["Alex", "alex", "Bo", "bo"],
    )


@pytest.mark.parametrize("question", RUN11)
async def test_a_web_question_about_a_person_goes_to_a_person(
    tmp_path: Path, question: str
) -> None:
    # No rule of the floor catches these: only the worker's judgement does.
    assert world_rule(question, ["Alex", "Bo"]) == ""
    candidate = Candidate(question, PLAN_PHRASES, audience=WEB, about_person=True)
    verdict = await _plan_gate(tmp_path).check(candidate)
    assert (verdict.accepted, verdict.route, verdict.route_reason) == (True, PERSON, ABOUT_PERSON)


async def test_a_thing_question_tagged_false_stays_web(tmp_path: Path) -> None:
    verdict = await _plan_gate(tmp_path).check(Candidate(THING, PLAN_PHRASES, about_person=False))
    assert (verdict.accepted, verdict.route, verdict.route_reason) == (True, WEB, "")


async def test_the_name_rule_holds_when_the_worker_says_false(tmp_path: Path) -> None:
    question = "Which endurance plan pace on the river loop suits Bo in the cold?"
    verdict = await _plan_gate(tmp_path).check(
        Candidate(question, PLAN_PHRASES, audience=WEB, about_person=False)
    )
    assert (verdict.accepted, verdict.route, verdict.route_reason) == (True, PERSON, RULE_PERSON)


async def test_a_person_question_about_a_person_needs_no_why(tmp_path: Path) -> None:
    candidate = Candidate(RUN11[0], PLAN_PHRASES, why="", audience=PERSON, about_person=True)
    verdict = await _plan_gate(tmp_path).check(candidate)
    assert (verdict.accepted, verdict.route, verdict.route_reason) == (True, PERSON, ABOUT_PERSON)


# -- a possessive title -----------------------------------------------------------

CALDO_TOPIC = Topic("recipes/alex-s-caldo-verde.md", "recipes", "Alex's Caldo Verde")
CALDO_BUNDLE = (
    "## recipes/alex-s-caldo-verde.md\n\n# Alex's Caldo Verde\n\n"
    "Lacinato kale, Yukon Gold potatoes, and chorizo. The base is chorizo fat.\n"
)
CALDO_PHRASES = ["Lacinato kale", "chorizo"]
# The five web questions of research run 12, with the roster of the tests.
RUN12_WEB = [
    "How should lacinato kale be blanched to keep its colour for a soup like Alex's Caldo Verde?",
    "How long is a potato, kale, and chorizo soup such as Alex's Caldo Verde safe in the fridge?",
    "Is chorizo fat or olive oil the better base for ALEX'S CALDO VERDE?",
    "How fine should lacinato kale be cut for Alex’s Caldo Verde to cook in five minutes?",
    "Which potato variety gives Alex's  caldo verde a smooth, thick broth?",
]
# The three person questions of run 12: each names a person in another way,
# or asks about us.
RUN12_PERSON = [
    ("Does Alex use homemade broth for Caldo Verde?", RULE_PERSON),
    ("Which chorizo does Bo buy for Alex's Caldo Verde?", RULE_PERSON),
    ("Do we freeze the leftover Alex's Caldo Verde with the kale in it?", RULE_OPENING),
]


def _caldo_gate(tmp_path: Path) -> Gate:
    people = [Person(id="alex", name="Alex"), Person(id="bo", name="Bo")]
    return Gate(
        bundle_text=CALDO_BUNDLE,
        terms=topic_names(CALDO_TOPIC, people=people),
        existing=[],
        store=FakeStore(),
        dup_sim=0.99,
        data_root=tmp_path,
        embed=_embed,
        people=["Alex", "alex", "Bo", "bo"],
        possessives=possessive_forms(CALDO_TOPIC.title, people),
        generic=generic_title(CALDO_TOPIC.title, people),
    )


@pytest.mark.parametrize("question", RUN12_WEB)
async def test_a_run_12_web_question_stays_web_with_the_generic_name(
    tmp_path: Path, question: str
) -> None:
    verdict = await _caldo_gate(tmp_path).check(Candidate(question, CALDO_PHRASES))
    assert verdict.accepted and verdict.route == WEB, verdict.reason
    assert verdict.generic_name
    assert "alex" not in verdict.candidate.question.casefold()
    assert "Caldo Verde?" in verdict.candidate.question or "Caldo Verde " in (
        verdict.candidate.question
    )


@pytest.mark.parametrize(("question", "rule"), RUN12_PERSON)
async def test_a_run_12_person_question_stays_person(
    tmp_path: Path, question: str, rule: str
) -> None:
    verdict = await _caldo_gate(tmp_path).check(Candidate(question, CALDO_PHRASES))
    assert verdict.accepted and verdict.route == PERSON
    assert verdict.route_reason == rule


async def test_the_generic_name_is_only_for_a_web_question(tmp_path: Path) -> None:
    gate = _caldo_gate(tmp_path)
    question = "Which stock does Alex's Caldo Verde take in the recipe page?"
    asked = await gate.check(Candidate(question, CALDO_PHRASES), person=True)
    assert asked.candidate.question == question and not asked.generic_name
    record = await _caldo_gate(tmp_path).check(
        Candidate("Which potato goes into Alex's Caldo Verde?", CALDO_PHRASES, audience=RECORD)
    )
    assert not record.generic_name and record.route == PERSON
    plain = await _caldo_gate(tmp_path).check(
        Candidate("What is a classic Caldo Verde broth?", CALDO_PHRASES)
    )
    assert plain.route == WEB and not plain.generic_name


def test_generic_question() -> None:
    forms = ["Alex's Caldo Verde"]
    assert generic_question("Is alex’s caldo verde spicy?", forms, "Caldo Verde") == (
        "Is Caldo Verde spicy?"
    )
    assert generic_question("Is Alex's Caldo Verde spicy?", forms, "") == (
        "Is Alex's Caldo Verde spicy?"
    )
    assert generic_question("Is Alex's stew spicy?", forms, "Caldo Verde") == (
        "Is Alex's stew spicy?"
    )
