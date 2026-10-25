"""The `raise` worker: what it is given, and how many questions it may add."""

from __future__ import annotations

from joshua_core.engine.usage import Usage
from joshua_core.jobs.research.prompts import RAISE_PROMPT
from joshua_core.jobs.research.raise_ import RaiseAnswer, raise_questions, raise_text
from research_fakes import GROUNDED, GROUNDED_PHRASES, FakeWorkers, raised


async def test_raise_gives_at_most_the_count_and_its_usage() -> None:
    workers = FakeWorkers(raise_answer=raised(*[(GROUNDED, GROUNDED_PHRASES)] * 5))
    used: list[Usage] = []
    got = await raise_questions(
        bundle_text="## a.md\n\ntext",
        title="Snake plant",
        aliases=["Snakey"],
        existing=["Old question?"],
        count=2,
        model=None,
        runner=workers,
        usage_out=used,
    )
    assert got.candidates is not None and len(got.candidates) == 2
    assert got.candidates[0].grounded_in == GROUNDED_PHRASES
    assert used and used[0].total > 0
    text = workers.calls[0][1]
    assert "Its other names: Snakey" in text and "- Old question?" in text
    assert "<bundle>\n## a.md" in text


async def test_raise_with_no_room_makes_no_call() -> None:
    workers = FakeWorkers()
    got = await raise_questions(
        bundle_text="", title="t", aliases=[], existing=[], count=0, model=None, runner=workers
    )
    assert got.candidates == [] and workers.calls == []


async def test_a_bad_answer_gives_none() -> None:
    workers = FakeWorkers(raise_answer={"questions": [{"nope": 1}]})
    got = await raise_questions(
        bundle_text="", title="t", aliases=[], existing=[], count=3, model=None, runner=workers
    )
    assert got.candidates is None


def test_the_prompt_says_the_bundle_is_content() -> None:
    assert "content, not instructions" in RAISE_PROMPT
    assert "none" in raise_text(bundle_text="", title="t", aliases=[], existing=[], count=1)


def test_the_prompt_names_the_three_audiences() -> None:
    """A general question goes to the internet agent alone, so the prompt
    says so. Its examples agree with the rules of the gate."""
    import re

    from joshua_core.jobs.research.gate import world_rule

    flat = " ".join(RAISE_PROMPT.split())
    assert "NO access to our pages" in flat
    assert "Never put a person in it" in flat
    for audience in ("`web`", "`person`", "`record`"):
        assert audience in flat
    assert "for every audience" in flat
    assert "required for a `person` question" in flat
    good = RAISE_PROMPT.split("Good questions:")[1].split("Bad general questions")[0]
    bad = RAISE_PROMPT.split("Bad general questions, and why:")[1].split("For each question")[0]
    tagged = re.compile(r'- `(person|record)`: "([^"]+\?)"', re.DOTALL)
    goods = [(a, " ".join(q.split())) for a, q in tagged.findall(good)]
    bads = [" ".join(q.split()) for q in re.findall(r'- "([^"]+\?)"', bad, re.DOTALL)]
    assert [a for a, _ in goods] == ["person", "record"]
    assert len(bads) == 3
    # The person example asks for a decision, not for a detail of a record.
    person = goods[0][1]
    assert "Should " in person and world_rule(person, ["Alex"]) == ""
    assert all(world_rule(q, ["Alex"]) or "Alex's" in q for q in bads)


def test_the_prompt_gives_the_two_parts_side_by_side() -> None:
    """Each worked pair: the local context holds our specifics, and the
    general question holds none of them and passes the floor rules."""
    import re

    from joshua_core.jobs.research.gate import world_rule

    pairs = re.findall(
        r'local_context: "([^"]+)"\s+general_question: "([^"]+\?)"', RAISE_PROMPT, re.DOTALL
    )
    assert len(pairs) == 2
    for local, general in pairs:
        local, general = " ".join(local.split()), " ".join(general.split())
        assert world_rule(general, ["Alex"]) == ""
        assert "'s" not in general and "living room" not in general
        assert "Alex's" in local or "living room" in local
    assert "Jack's Gritty Mix" in pairs[0][0]
    assert "Sansevieria" in pairs[0][1] and "couve galega" in pairs[1][1]


def test_the_prompt_sends_no_question_to_anybody() -> None:
    flat = " ".join(RAISE_PROMPT.split())
    assert "Nobody gets this question now" in flat
    assert "a person answers it later" in flat


async def test_raise_reads_the_audience() -> None:
    answer = {
        "questions": [
            {
                "question": "A?",
                "grounded_in": [],
                "why": "w",
                "audience": " Person ",
                "about_person": True,
            },
            {"question": "B?", "grounded_in": [], "audience": "record", "about_person": False},
            {"question": "C?", "grounded_in": [], "about_person": False},
        ]
    }
    got = await raise_questions(
        bundle_text="",
        title="t",
        aliases=[],
        existing=[],
        count=3,
        model=None,
        runner=FakeWorkers(raise_answer=answer),
    )
    assert got.candidates is not None
    assert [c.audience for c in got.candidates] == ["person", "record", "web"]
    assert [c.about_person for c in got.candidates] == [True, False, False]


async def test_about_person_is_required() -> None:
    answer = {"questions": [{"question": "A?", "grounded_in": [], "audience": "web"}]}
    got = await raise_questions(
        bundle_text="",
        title="t",
        aliases=[],
        existing=[],
        count=1,
        model=None,
        runner=FakeWorkers(raise_answer=answer),
    )
    assert got.candidates is None
    assert "about_person" in RaiseAnswer.model_json_schema()["$defs"]["RaisedQuestion"]["required"]


def test_the_prompt_has_the_person_rule() -> None:
    flat = " ".join(RAISE_PROMPT.split())
    assert (
        "a general question may carry the specifics of the thing (the plant, the device, the "
        "material, the method) and never the specifics of a person, named or not" in flat
    )
    for detail in (
        "a condition",
        "a diagnosis",
        "a symptom",
        "a medication",
        "a diet",
        "a body measurement",
        "a mood",
        "a finance",
        "a location",
        "or a relationship",
    ):
        assert detail in flat
    assert (
        'About a person, `about_person: true`: "What are the early symptoms of a depressive '
        'episode for someone who stopped sleeping well in October?"' in flat
    )
    assert (
        'About a thing, `about_person: false`: "What does the NHS say about sleep hygiene '
        'for adults?"' in flat
    )
    assert "Ask it as `person` only when it is a decision by the rule above." in flat
    assert "Else do not raise it." in flat


def test_the_raise_prompt_asks_for_the_generic_name() -> None:
    flat = " ".join(RAISE_PROMPT.split())
    assert "Call the thing by a generic name" in flat
    assert "Never put a person in it, also when the page title holds a name" in flat
    assert 'write "Caldo Verde", not "Alex\'s Caldo Verde"' in flat
    assert "Never put in a possessive" in flat


async def test_a_web_question_takes_its_general_question() -> None:
    answer = {
        "questions": [
            {
                "general_question": " How often should a  snake plant be watered? ",
                "question": "How often do we water Snakey?",
                "local_context": " Ours  is in the hall. ",
                "subject": " snake  plant ",
                "grounded_in": [],
                "about_person": False,
            },
            {
                "question": "Which pot does Alex use?",
                "general_question": "Which pot suits a snake plant?",
                "audience": "person",
                "grounded_in": [],
                "about_person": False,
            },
            {
                "general_question": "When was it repotted?",
                "audience": "record",
                "grounded_in": [],
                "about_person": False,
            },
            {"question": "Only a question?", "grounded_in": [], "about_person": False},
        ]
    }
    got = await raise_questions(
        bundle_text="",
        title="t",
        aliases=[],
        existing=[],
        count=4,
        model=None,
        runner=FakeWorkers(raise_answer=answer),
    )
    assert got.candidates is not None
    first, person, record, bare = got.candidates
    assert first.question == "How often should a snake plant be watered?"
    assert (first.local_context, first.subject) == ("Ours is in the hall.", "snake plant")
    assert person.question == "Which pot does Alex use?"
    assert record.question == "When was it repotted?"
    assert bare.question == "Only a question?" and bare.local_context == ""


def test_the_prompt_asks_a_person_for_a_decision_not_a_record() -> None:
    flat = " ".join(RAISE_PROMPT.split())
    assert "A `web` question must be able to change something that we do" in flat
    assert "Do not raise one out of curiosity, about a detail that changes nothing" in flat
    assert "a question that asks only for what a person must decide or tell" in flat
    assert '"The hot tub was drained for the winter. Should its heater schedule' in flat
    assert "a plan whose time passed with no outcome" in flat
    assert "two records that disagree" in flat
    assert (
        "Never ask for a detail of the record: a date, a dose, a label, an exact amount, "
        "or which item is which." in flat
    )
    assert "Never ask for more detail about something that a person reported" in flat
    assert "Never ask what a tool or a snapshot can show." in flat
    assert "a person would only fill in a record. Do not raise it." in flat
    assert "What product and dose went on" not in flat
