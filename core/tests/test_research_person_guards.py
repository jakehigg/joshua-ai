"""The person guards of the `research` job: a page about a person is not a
topic, and a topic in a person section gets no web question. No model, no
network, no database."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from jobs_fakes import CONFIG, fixed_clock
from joshua_core.jobs.research.dossier import Question, load_dossier, new_dossier, save_dossier
from joshua_core.jobs.research.gate import ABOUT_PERSON, PERSON_SECTION
from joshua_core.jobs.research.job import ResearchJob
from joshua_core.jobs.research.questions import load_questions
from joshua_core.jobs.runner import JobArgsError
from joshua_core.memory import embed as embed_module
from joshua_shared import config as config_module
from research_fakes import (
    RESEARCH_ON,
    SNAKE,
    WIKI,
    FakeInternet,
    FakeStore,
    FakeWorkers,
    build_research_wiki,
)

ROSTER = CONFIG + "  - id: bo\n    name: Bo\n"
COACH = "coach/walking-plan.md"
WALK_PAGE = (
    "# Walking plan\n\n"
    "The plan is the river loop, 3 miles, at dawn, on a gravel path.\n"
    "Alex walks it on weekdays. Bo joins on Sundays.\n"
)
PHRASES = ["river loop", "3 miles"]
SHOES = "What shoe cushioning suits a walking plan of 3 miles on a gravel river loop?"
LAYERS = "Which winter layers suit a walking plan on the river loop at 3 miles at dawn?"


def _vector(text: str) -> list[float]:
    """One dimension for each text, so no two texts are near."""
    vec = [0.0] * 1024
    vec[int(hashlib.sha256(text.encode()).hexdigest(), 16) % 1024] = 1.0
    return vec


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": _vector(text))


def raised(*questions: str, audience: str = "web", about_person: bool = False) -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": q,
                "grounded_in": PHRASES,
                "why": "",
                "audience": audience,
                "about_person": about_person,
            }
            for q in questions
        ]
    }


class Harness:
    def __init__(self, data: Path, pages: dict[str, str], answer: dict[str, Any]) -> None:
        self.data = data
        self.wiki = build_research_wiki(data, {**WIKI, **pages})
        self.settings = config_module.parse(ROSTER + RESEARCH_ON, env={}, source="<test>")
        self.workers = FakeWorkers(raise_answer=answer)
        self.internet = FakeInternet()

    def job(self) -> ResearchJob:
        return ResearchJob(
            store=FakeStore(),
            settings=self.settings,
            data_dir=self.data,
            runner=self.workers,
            internet_runner=self.internet,
        )

    async def run(self, **args: Any):
        job = self.job()
        return await job.run_once(args=job.check_args(args), dry_run=False, clock=fixed_clock())


def _section(report: Any, heading: str) -> str:
    return dict(report.sections)[heading]


async def test_a_page_about_a_person_is_never_the_topic(tmp_path: Path) -> None:
    """A page titled with a name, named in many journal entries, is not a
    topic, and the entries that only name a person count for no topic."""
    pages = {"notes/alex-notes.md": "# Alex\n\nPrefers mornings.\n"}
    for n in range(1, 13):
        pages[f"journal/2026/10/{n:02d}/day-{n}.md"] = f"Alex and Bo talked, day {n}.\n"
    h = Harness(tmp_path / "data", pages, raised())
    report = await h.run(mode="plan")
    assert (report.data["topic"], report.data["rule"]) == (SNAKE, "journal")
    assert report.data["reason"].startswith("1 journal entries")
    with pytest.raises(JobArgsError, match="not a topic page"):
        h.job().check_args({"mode": "plan", "topic": "notes/alex-notes.md"})


async def test_a_person_section_topic_sends_each_raised_question_to_a_person(
    tmp_path: Path,
) -> None:
    h = Harness(
        tmp_path / "data", {"coach.md": "# Coach\n", COACH: WALK_PAGE}, raised(SHOES, LAYERS)
    )
    report = await h.run(mode="plan", topic=COACH)
    pages = load_questions(h.data, COACH)
    assert sorted(p.page.question for p in pages) == sorted([SHOES, LAYERS])
    assert {p.page.route_reason for p in pages} == {PERSON_SECTION}
    dossier = load_dossier(COACH, h.data)
    assert dossier is not None and dossier.questions == []
    assert "- Person section: `coach`." in _section(report, "Topic")
    assert f"; {PERSON_SECTION})" in _section(report, "For a person")


async def test_a_run_on_a_person_section_researches_only_what_a_person_asked(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path / "data", {"coach.md": "# Coach\n", COACH: WALK_PAGE}, raised())
    dossier = new_dossier(COACH, "Walking plan")
    dossier.questions.append(Question(slug="shoes", question=SHOES, grounded_in=PHRASES))
    dossier.questions.append(Question(slug="layers", question=LAYERS, asked_by="alex"))
    save_dossier(dossier, h.data, {})
    report = await h.run(mode="run", topic=COACH)
    assert "`shoes` is a question of a person section" in _section(report, "Notes")
    assert all(SHOES not in text for text in h.internet.texts)
    assert any(LAYERS in text for text in h.internet.texts)


async def test_a_topic_outside_a_person_section_keeps_its_web_questions(tmp_path: Path) -> None:
    h = Harness(
        tmp_path / "data",
        {"fitness.md": "# Fitness\n", "fitness/walking-plan.md": WALK_PAGE},
        raised(SHOES),
    )
    report = await h.run(mode="plan", topic="fitness/walking-plan.md")
    dossier = load_dossier("fitness/walking-plan.md", h.data)
    assert dossier is not None and [q.question for q in dossier.questions] == [SHOES]
    assert "Person section" not in _section(report, "Topic")


# -- about a person -----------------------------------------------------------

PLAN = "fitness/endurance-plan.md"
PLAN_PAGE = (
    "# Endurance plan\n\n"
    "The plan is the river loop, 3 miles, at dawn, on a gravel path.\n"
    "Alex trains on weekdays. Bo joins on Sundays.\n"
)
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


async def test_the_run_11_questions_never_reach_the_web(tmp_path: Path) -> None:
    answer = raised(*RUN11, about_person=True)
    answer["questions"].append(raised(THING)["questions"][0])
    h = Harness(tmp_path / "data", {"fitness.md": "# Fitness\n", PLAN: PLAN_PAGE}, answer)
    report = await h.run(mode="plan", topic=PLAN)
    dossier = load_dossier(PLAN, h.data)
    assert dossier is not None and [q.question for q in dossier.questions] == [THING]
    pages = {p.page.question: p.page for p in load_questions(h.data, PLAN)}
    dropped = {r["question"] for r in report.data["rejected"]}
    dropped |= {r["question"] for r in report.data["asked_before"]}
    for question in RUN11:
        assert (question in pages) != (question in dropped), question
    assert sorted(pages) == sorted(RUN11)
    for page in pages.values():
        assert page.route_reason == ABOUT_PERSON
        assert (
            page.why == "Only somebody here knows this: the web cannot answer a question about us."
        )
    assert f"; {ABOUT_PERSON})" in _section(report, "For a person")
