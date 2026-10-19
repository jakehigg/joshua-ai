"""The `research` job on a fixture wiki: the arguments, `plan`, `run`, a dry
run, and the rules it never breaks. No model, no network, no database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jobs_fakes import FakeJobRunStore, fixed_clock
from joshua_core.engine import agent
from joshua_core.jobs import JobArgsError, JobRunner, build_job_runner
from joshua_core.jobs.research.dossier import load_dossier
from joshua_core.jobs.research.job import ResearchJob, parse_count, parse_slugs, worker_model
from joshua_core.memory import embed as embed_module
from joshua_shared import frontmatter, layout
from research_fakes import (
    EDITOR_USAGE,
    GENERIC,
    GROUNDED,
    GROUNDED_PHRASES,
    LEAF,
    LOOKUP_USAGE,
    RAISE_USAGE,
    SNAKE,
    FakeInternet,
    FakeStore,
    FakeWorkers,
    build_research_wiki,
    changed,
    editor,
    raised,
    research_settings,
    snapshot,
    word_vector,
)

SECOND = "When should a snake plant be repotted when leaf flop shows up in a dim hall?"
SECOND_PHRASES = ["leaf flop", "lives in the hall"]
THIRD = "Does the snake plant need more light than the hall gives it in winter?"
THIRD_PHRASES = ["lives in the hall", "65 to 68 F in winter"]


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": word_vector(text))


class Harness:
    def __init__(self, data: Path, extra: str = "") -> None:
        self.data = data
        self.wiki = build_research_wiki(data)
        self.settings = research_settings(extra)
        self.store = FakeStore()
        self.workers = FakeWorkers(
            raise_answer=raised(
                (GROUNDED, GROUNDED_PHRASES),
                (GENERIC, []),
                (SECOND, SECOND_PHRASES),
                (THIRD, THIRD_PHRASES),
            )
        )
        self.internet = FakeInternet()

    def job(self) -> ResearchJob:
        return ResearchJob(
            store=self.store,
            settings=self.settings,
            data_dir=self.data,
            runner=self.workers,
            internet_runner=self.internet,
        )

    async def run(self, *, dry_run: bool = False, **args: Any):
        job = self.job()
        return await job.run_once(args=job.check_args(args), dry_run=dry_run, clock=fixed_clock())

    def dossier(self):
        return load_dossier(SNAKE, self.data)


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "data")


# -- arguments --------------------------------------------------------------------


def test_the_arguments(h: Harness) -> None:
    job = h.job()
    assert job.check_args({"mode": "plan"}) == {"mode": "plan"}
    assert job.check_args({"mode": "plan", "topic": f" {SNAKE} "}) == {
        "mode": "plan",
        "topic": SNAKE,
    }
    assert job.check_args({"mode": "run", "max_questions": "2"})["max_questions"] == 2
    for bad in (
        {},
        {"mode": "go"},
        {"mode": "plan", "topic": "houseplants.md"},
        {"mode": "plan", "topic": "journal/2026/10/15/snake-plant-leaf-flop.md"},
        {"mode": "plan", "topic": "nope.md"},
        {"mode": "plan", "bogus": 1},
        {"mode": "plan", "topic": SNAKE, "questions": "a"},
        {"mode": "run", "questions": "a"},
        {"mode": "run", "topic": SNAKE, "questions": "not-in-the-dossier"},
        {"mode": "run", "max_questions": 0},
        {"mode": "run", "max_questions": "x"},
    ):
        with pytest.raises(JobArgsError):
            job.check_args(bad)


def test_a_run_with_the_internet_off_is_refused(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", "internet:\n  enabled: false\n")
    with pytest.raises(JobArgsError, match="internet.enabled is false"):
        h.job().check_args({"mode": "run"})
    assert h.job().check_args({"mode": "plan"}) == {"mode": "plan"}


def test_parse_helpers() -> None:
    assert parse_slugs("a, b,a") == ["a", "b"]
    assert parse_slugs(["a"]) == ["a"]
    for bad in (3, "", "Bad Slug", [1]):
        with pytest.raises(JobArgsError):
            parse_slugs(bad)
    assert parse_count(3) == 3
    for bad in (True, 1.5, None, -1):
        with pytest.raises(JobArgsError):
            parse_count(bad)


def test_the_model_chain() -> None:
    assert worker_model(research_settings("    model: m1\n")) == "m1"
    assert worker_model(research_settings()) == research_settings().core.reflection_model


# -- plan -------------------------------------------------------------------------


async def test_plan_picks_the_journal_topic_and_writes_only_the_dossier_and_readme(
    h: Harness,
) -> None:
    before = snapshot(h.wiki)
    report = await h.run(mode="plan")
    assert changed(before, snapshot(h.wiki)) == {
        "knowledge/houseplants/snake-plant.md",
        "knowledge/README.md",
    }
    assert sorted(p.relative_to(h.wiki).as_posix() for p in report.written) == [
        "knowledge/README.md",
        "knowledge/houseplants/snake-plant.md",
    ]
    assert report.data["rule"] == "journal"
    assert report.data["topic"] == SNAKE
    assert LEAF in report.data["bundle"]["paths"]
    assert h.internet.texts == []
    assert h.workers.names() == ["raise"]
    dossier = h.dossier()
    assert dossier is not None
    assert [q.question for q in dossier.questions] == [GROUNDED, SECOND, THIRD]
    assert all(q.status == "open" and q.asked_by == "research" for q in dossier.questions)
    assert report.data["slugs"] == [q.slug for q in dossier.questions]
    rejected = report.data["rejected"]
    assert [r["question"] for r in rejected] == [GENERIC]
    assert "grounded_in" in rejected[0]["reason"]
    body = dict(report.sections)
    assert "Runner-up" in body["Topic"]
    assert GENERIC in body["Rejected"]
    assert report.calls == 1 and report.tokens == RAISE_USAGE.total


async def test_plan_rejects_a_question_that_names_a_person_on_the_roster(h: Harness) -> None:
    named = "Will Alex check the snake plant for leaf flop in the hall?"
    h.workers = FakeWorkers(raise_answer=raised((named, SECOND_PHRASES), (THIRD, THIRD_PHRASES)))
    report = await h.run(mode="plan", topic=SNAKE)
    rejected = report.data["rejected"]
    assert [r["question"] for r in rejected] == [named]
    assert "names a person" in rejected[0]["reason"]
    assert [q.question for q in h.dossier().questions] == [THIRD]


async def test_plan_dry_run_writes_nothing(h: Harness) -> None:
    before = snapshot(h.wiki)
    report = await h.run(mode="plan", dry_run=True)
    assert snapshot(h.wiki) == before
    assert report.written == []
    assert len(report.data["added"]) == 3


async def test_plan_does_not_raise_when_enough_questions_are_open(h: Harness) -> None:
    await h.run(mode="plan")
    await h.run(mode="plan")
    assert h.workers.names() == ["raise"]


async def test_plan_with_no_answer_from_raise_says_so(h: Harness) -> None:
    h.workers.raise_answer = {"questions": "not a list"}
    report = await h.run(mode="plan", dry_run=True)
    assert "no answer" in dict(report.sections)["Notes"]


async def test_no_topic_gives_an_empty_report(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", "    coverage_target: 0.0\n")
    for rel in list(h.wiki.rglob("*.md")):
        if rel.relative_to(h.wiki).parts[0] == "journal":
            rel.unlink()
    report = await h.run(mode="plan")
    assert report.data["topic"] is None
    assert report.written == []
    assert report.summary.startswith("No topic to research.")


# -- run --------------------------------------------------------------------------


async def test_run_writes_pages_the_dossier_and_the_readme(h: Harness) -> None:
    before = snapshot(h.wiki)
    report = await h.run(mode="run", topic=SNAKE)
    dossier = h.dossier()
    assert dossier is not None
    slugs = [q.slug for q in dossier.questions]
    pages = {f"knowledge/houseplants/snake-plant/{s}.md" for s in slugs}
    assert changed(before, snapshot(h.wiki)) == pages | {
        "knowledge/houseplants/snake-plant.md",
        "knowledge/README.md",
    }
    assert {p.relative_to(h.wiki).as_posix() for p in report.written} == pages | {
        "knowledge/houseplants/snake-plant.md",
        "knowledge/README.md",
    }
    assert all(q.status == "answered" for q in dossier.questions)
    assert dossier.coverage == 1.0
    assert dossier.runs == 1
    assert str(dossier.researched_at) == "2026-10-19"
    assert str(dossier.refresh_after) == "2027-04-17"
    page = (h.wiki / f"knowledge/houseplants/snake-plant/{slugs[0]}.md").read_text()
    meta, _ = frontmatter.split(page)
    assert meta["context"] == [SNAKE]
    assert meta["grounded_in"] == GROUNDED_PHRASES
    second = frontmatter.split(
        (h.wiki / f"knowledge/houseplants/snake-plant/{slugs[1]}.md").read_text()
    )[0]
    assert second["context"] == [LEAF, SNAKE]
    readme = layout.knowledge_index(h.data).read_text()
    assert "| 1.00 | 3/3 | 2026-10-19 | 0 |" in readme
    dossier_text = layout.knowledge_dossier(SNAKE, h.data).read_text()
    assert "About every 4 weeks in winter" in dossier_text
    assert report.summary.startswith(f"research: {SNAKE} (3 pages, 0 held, 0 dropped)")


async def test_run_tokens_are_the_sum_over_every_worker(h: Harness) -> None:
    report = await h.run(mode="run", topic=SNAKE, max_questions=1)
    expected = RAISE_USAGE.total + 2 * LOOKUP_USAGE.total + EDITOR_USAGE.total
    assert report.tokens == expected
    assert report.calls == 4
    assert report.data["usage"] == {
        "editor": {"calls": 1, "tokens": EDITOR_USAGE.total},
        "internet": {"calls": 2, "tokens": 2 * LOOKUP_USAGE.total},
        "raise": {"calls": 1, "tokens": RAISE_USAGE.total},
    }


async def test_max_questions_caps_the_run(h: Harness) -> None:
    report = await h.run(mode="run", topic=SNAKE, max_questions=1)
    assert len(report.data["pages"]) == 1
    dossier = h.dossier()
    assert dossier is not None
    assert [q.status for q in dossier.questions] == ["answered", "open", "open"]
    assert dossier.coverage == pytest.approx(0.3333, abs=1e-4)


async def test_questions_picks_exactly_those(h: Harness) -> None:
    await h.run(mode="plan", topic=SNAKE)
    dossier = h.dossier()
    assert dossier is not None
    wanted = dossier.questions[2].slug
    report = await h.run(mode="run", topic=SNAKE, questions=wanted)
    assert report.data["pages"] == [f"knowledge/houseplants/snake-plant/{wanted}.md"]
    assert h.workers.names() == ["raise", "editor"]
    assert h.internet.texts[0].startswith(THIRD)


async def test_dry_run_writes_nothing_and_shows_each_page(h: Harness) -> None:
    before = snapshot(h.wiki)
    report = await h.run(mode="run", topic=SNAKE, dry_run=True)
    assert snapshot(h.wiki) == before
    assert report.written == []
    pages = dict(report.sections)["Pages"]
    assert pages.count("```markdown") == 3
    assert "## Where this differs for us" in pages
    assert "would write" in dict(report.sections)["Researched"]


async def test_a_page_with_one_source_is_never_written(h: Harness) -> None:
    from research_fakes import PRIMARY

    h.workers.editor_answer = editor((PRIMARY, "primary"))
    before = snapshot(h.wiki)
    report = await h.run(mode="run", topic=SNAKE, max_questions=1)
    after = snapshot(h.wiki)
    assert not any("snake-plant/" in rel for rel in changed(before, after))
    dossier = h.dossier()
    assert dossier is not None
    first = dossier.questions[0]
    assert first.status == "held" and first.holds == 1
    assert "independent sources" in first.hold_reason
    assert report.data["held"] == [first.slug]
    assert "Held:" in layout.knowledge_dossier(SNAKE, h.data).read_text()


async def test_two_holds_drop_a_question(h: Harness) -> None:
    h.workers.editor_answer = editor(verdict="hold", reason="weak")
    await h.run(mode="plan", topic=SNAKE)
    slug = h.dossier().questions[0].slug  # type: ignore[union-attr]
    await h.run(mode="run", topic=SNAKE, questions=slug)
    assert h.dossier().find(slug).status == "held"  # type: ignore[union-attr]
    report = await h.run(mode="run", topic=SNAKE, questions=slug)
    question = h.dossier().find(slug)  # type: ignore[union-attr]
    assert question.status == "dropped" and question.holds == 2
    assert report.data["dropped"] == [slug]
    with pytest.raises(JobArgsError):
        h.job().check_args({"mode": "run", "topic": "garden/roses.md", "questions": slug})


async def test_a_person_question_is_researched_first(h: Harness) -> None:
    await h.run(mode="plan", topic=SNAKE)
    dossier = h.dossier()
    assert dossier is not None
    from joshua_core.jobs.research.dossier import Question, save_dossier

    dossier.questions.append(
        Question(slug="asked", question="Is the snake plant safe for a cat?", asked_by="alex")
    )
    save_dossier(dossier, h.data, {})
    report = await h.run(mode="run", max_questions=1)
    assert report.data["rule"] == "person"
    assert report.data["pages"] == ["knowledge/houseplants/snake-plant/asked.md"]


# -- what it never does -----------------------------------------------------------


async def test_the_bundle_never_reaches_a_lookup(h: Harness) -> None:
    await h.run(mode="run", topic=SNAKE)
    assert h.internet.texts
    secret = "We tied the leaves and moved it away from the door"
    for kwargs in h.internet.kwargs:
        text = kwargs["user_text"]
        assert secret not in text
        assert "## houseplants/snake-plant.md" not in text
        assert "<bundle>" not in text
        assert "Pothos" not in text
    assert any(secret in text for _, text in h.workers.calls if _ == "raise")


async def test_a_page_path_never_leaves_knowledge(h: Harness) -> None:
    report = await h.run(mode="run", topic=SNAKE)
    root = layout.knowledge_root(h.data).resolve()
    assert report.written
    for path in report.written:
        assert path.resolve().is_relative_to(root)


async def test_a_slug_that_leaves_knowledge_is_refused(h: Harness) -> None:
    from joshua_core.jobs.research.write import page_for

    for slug in ("../../Home", "a/b", ".hidden"):
        with pytest.raises(ValueError):
            page_for(SNAKE, slug, h.data, "x")


def test_the_agent_still_holds_no_tools() -> None:
    assert agent._NO_TOOLS == []


# -- the runner ---------------------------------------------------------------------


def test_the_runner_has_the_job_only_when_it_is_on(tmp_path: Path) -> None:
    import asyncio

    def runner(settings: Any) -> JobRunner:
        return build_job_runner(
            repo=FakeJobRunStore(),
            store=FakeStore(),
            settings=settings,
            data_dir=tmp_path,
            lock=asyncio.Lock(),
        )

    assert runner(research_settings()).has("research")
    off = research_settings()
    off.jobs.research.enabled = False
    assert not runner(off).has("research")
