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
    """A `web` question goes to the internet agent alone, so the prompt says
    so. Its examples agree with the route of the gate."""
    import re

    from joshua_core.jobs.research.gate import world_rule

    flat = " ".join(RAISE_PROMPT.split())
    assert "NO access to our pages" in flat
    assert "never names a person" in flat
    for audience in ("`web`", "`person`", "`record`"):
        assert audience in flat
    assert "for every audience" in flat
    assert "required for a `person` question" in flat
    good = RAISE_PROMPT.split("Good questions:")[1].split("Bad questions")[0]
    bad = RAISE_PROMPT.split("Bad questions, and why:")[1].split("For each question")[0]
    tagged = re.compile(r'- `(web|person|record)`: "([^"]+\?)"', re.DOTALL)
    goods = [(a, " ".join(q.split())) for a, q in tagged.findall(good)]
    bads = [(a, " ".join(q.split())) for a, q in tagged.findall(bad)]
    assert [a for a, _ in goods] == ["web", "web", "person", "record"]
    assert len(bads) == 2
    for audience, question in goods:
        assert bool(world_rule(question, ["Alex"])) == (audience == "person"), question
    assert all(world_rule(q, ["Alex"]) for _, q in bads)


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
        "a `web` question may carry the specifics of the thing (the plant, the device, the "
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
    assert "Ask it as `person` when a person here can answer it" in flat
    assert "When no person can answer it, do not raise it" in flat


def test_the_raise_prompt_asks_for_the_generic_name() -> None:
    flat = " ".join(RAISE_PROMPT.split())
    assert "Call a thing by its generic name." in flat
    assert "Never put a person's name in a `web` question" in flat
    assert 'write "Caldo Verde", not "Alex\'s Caldo Verde"' in flat
