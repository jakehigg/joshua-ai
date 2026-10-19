"""The route of the `research` job: a `web` question goes to the dossier, a
`person` question becomes a page under `wiki/questions/`, and a `record`
question that our records answer is closed in the report. No model, no
network, no database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jobs_fakes import fixed_clock
from joshua_core.jobs.research.dossier import load_dossier
from joshua_core.jobs.research.job import ResearchJob
from joshua_core.jobs.research.questions import load_questions, set_answer
from joshua_core.memory import embed as embed_module
from joshua_shared import frontmatter, layout
from research_fakes import (
    GROUNDED,
    GROUNDED_PHRASES,
    LEAF,
    SNAKE,
    FakeInternet,
    FakeStore,
    FakeWorkers,
    build_research_wiki,
    changed,
    research_settings,
    snapshot,
    word_vector,
)

PERSON_Q = "What dose of feed went on the snake plant on 2026-10-04 in the gritty soil mix?"
PERSON_PHRASES = ["gritty soil mix", "water to runoff"]
RECORD_Q = "When was the snake plant last tied up after the leaf flop in the hall?"
RECORD_PHRASES = ["leaf flop", "lives in the hall"]
ASK_Q = "Which pot does the snake plant that lives in the hall sit in during winter?"
ASK_PHRASES = ["lives in the hall", "65 to 68 F in winter"]


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": word_vector(text))


def _raised(*items: tuple[str, list[str], str, str]) -> dict[str, Any]:
    return {
        "questions": [
            {"question": q, "grounded_in": g, "why": why, "audience": a} for q, g, why, a in items
        ]
    }


ANSWER = _raised(
    (GROUNDED, GROUNDED_PHRASES, "It sets the cadence.", "web"),
    (PERSON_Q, PERSON_PHRASES, "", "web"),
    (RECORD_Q, RECORD_PHRASES, "", "record"),
    (ASK_Q, ASK_PHRASES, "The pot size sets how fast the mix dries.", "person"),
)


class Harness:
    def __init__(self, data: Path, near: dict[str, float] | None = None) -> None:
        self.data = data
        self.wiki = build_research_wiki(data)
        self.settings = research_settings()
        self.store = FakeStore(near if near is not None else {f"wiki/{LEAF}": 0.9})
        self.workers = FakeWorkers(raise_answer=ANSWER)
        self.internet = FakeInternet()

    async def run(self, *, dry_run: bool = False, **args: Any):
        job = ResearchJob(
            store=self.store,
            settings=self.settings,
            data_dir=self.data,
            runner=self.workers,
            internet_runner=self.internet,
        )
        return await job.run_once(args=job.check_args(args), dry_run=dry_run, clock=fixed_clock())


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "data")


async def test_plan_routes_each_question(h: Harness) -> None:
    before = snapshot(h.wiki)
    report = await h.run(mode="plan", topic=SNAKE)
    pages = load_questions(h.data, SNAKE)
    assert sorted(p.page.question for p in pages) == sorted([PERSON_Q, ASK_Q])
    ids = report.data["person"]
    assert sorted(ids) == sorted(p.id for p in pages)
    assert all(i.startswith("houseplants/") for i in ids)
    assert report.data["record"] == [LEAF]
    assert report.data["rejected"] == []
    assert changed(before, snapshot(h.wiki)) == {
        "knowledge/houseplants/snake-plant.md",
        "knowledge/README.md",
        "questions/README.md",
        *(f"questions/{i}.md" for i in ids),
    }
    # The dossier takes the web question only, and keeps the counts.
    dossier = load_dossier(SNAKE, h.data)
    assert dossier is not None
    assert [q.question for q in dossier.questions] == [GROUNDED]
    assert (dossier.person_open, dossier.person_answered) == (2, 0)
    body = layout.knowledge_dossier(SNAKE, h.data).read_text()
    assert "Questions for a person: 2 open, 0 answered." in body
    assert "(../../questions/README.md)" in body


async def test_the_question_page(h: Harness) -> None:
    await h.run(mode="plan", topic=SNAKE)
    [dose] = [p for p in load_questions(h.data, SNAKE) if p.page.question == PERSON_Q]
    page = dose.page
    assert page.route_reason == "names a record date"
    assert page.for_ == "everyone"
    assert page.status == "open" and page.asked_by == "research"
    assert page.raised_at.isoformat() == "2026-10-19"
    assert page.context == [SNAKE]
    assert page.grounded_in == PERSON_PHRASES
    assert "web cannot answer" in page.why
    meta, body = frontmatter.split(dose.text)
    assert meta["for"] == "everyone" and meta["answer"] is None
    assert body.startswith(f"# {PERSON_Q}\n")
    assert "## Why Joshua wants to know" in body
    assert "## Where it came from" in body and "(../../houseplants/snake-plant.md)" in body
    assert body.rstrip().endswith("## Answer")
    [ask] = [p for p in load_questions(h.data, SNAKE) if p.page.question == ASK_Q]
    assert ask.page.route_reason == "the raise worker asks a person"
    assert ask.page.why == "The pot size sets how fast the mix dries."


async def test_the_report_sections(h: Harness) -> None:
    report = await h.run(mode="plan", topic=SNAKE)
    sections = dict(report.sections)
    names = [name for name, _ in report.sections]
    assert names[:6] == [
        "Topic",
        "Bundle",
        "Questions",
        "For a person",
        "Answered by our records",
        "Rejected",
    ]
    assert GROUNDED in sections["Questions"] and PERSON_Q not in sections["Questions"]
    person = sections["For a person"]
    assert PERSON_Q in person and "names a record date" in person and "wrote `questions/" in person
    assert "Why:" in person
    assert f"answered_by: `{LEAF}`" in sections["Answered by our records"]
    assert sections["Rejected"] == "None."
    assert "2 for a person, 1 answered by our records" in report.summary


async def test_a_dry_run_writes_no_question_page(h: Harness) -> None:
    before = snapshot(h.wiki)
    report = await h.run(mode="plan", topic=SNAKE, dry_run=True)
    assert snapshot(h.wiki) == before
    assert not layout.questions_root(h.data).exists()
    assert report.written == []
    assert len(report.data["person"]) == 2
    assert "would write `questions/" in dict(report.sections)["For a person"]


async def test_a_run_report_has_the_new_sections(h: Harness) -> None:
    report = await h.run(mode="run", topic=SNAKE)
    names = [name for name, _ in report.sections]
    assert "For a person" in names and "Answered by our records" in names
    assert "Researched" in names
    assert len(report.data["person"]) == 2
    assert all(PERSON_Q not in text for text in h.internet.texts)
    assert all(ASK_Q not in text for text in h.internet.texts)


async def test_a_record_question_with_no_record_becomes_a_page(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", near={})
    report = await h.run(mode="plan", topic=SNAKE)
    assert report.data["record"] == []
    [page] = [p for p in load_questions(h.data, SNAKE) if p.page.question == RECORD_Q]
    assert page.page.route_reason == "no record found"
    assert "did not find it" in page.page.why


async def test_a_question_asked_before_is_not_written_again(h: Harness) -> None:
    await h.run(mode="plan", topic=SNAKE)
    first = {p.id: p for p in load_questions(h.data, SNAKE)}
    [dose] = [p for p in first.values() if p.page.question == PERSON_Q]
    set_answer(
        dose, h.data, status="dismissed", answer="Not tracked.", by="alex", day=dose.page.raised_at
    )
    h.workers = FakeWorkers(raise_answer=ANSWER)
    report = await h.run(mode="plan", topic=SNAKE)
    assert report.data["person"] == []
    reasons = {a["question"]: a["reason"] for a in report.data["asked_before"]}
    assert reasons == {PERSON_Q: "dismissed before", ASK_Q: "already asked"}
    assert {p.id for p in load_questions(h.data, SNAKE)} == set(first)
    person = dict(report.sections)["For a person"]
    assert "already asked" in person and "dismissed before" in person
    dossier = load_dossier(SNAKE, h.data)
    assert dossier is not None and dossier.person_open == 1
