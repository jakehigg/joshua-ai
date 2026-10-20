"""The insight pass at the end of a research run: the same run, the same
commit, the usage under `insight`, the `then_insights` argument, the dry run,
and a failure that never fails the research run."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from insights_fakes import INSIGHT_USAGE, FakeInsightWorker, answer, item
from jobs_fakes import FakeJobRunStore, fixed_clock
from joshua_core.jobs import JobArgsError, JobRunner
from joshua_core.jobs.insights.job import InsightPass
from joshua_core.jobs.insights.page import load_insights
from joshua_core.jobs.insights.prompts import INSIGHT_PROMPT
from joshua_core.jobs.research.job import ResearchJob, parse_flag
from joshua_core.memory import embed as embed_module
from joshua_shared import layout, wikigit
from research_fakes import (
    GROUNDED,
    GROUNDED_PHRASES,
    SNAKE,
    FakeInternet,
    FakeStore,
    FakeWorkers,
    build_research_wiki,
    changed,
    raised,
    research_settings,
    snapshot,
    word_vector,
)

INSIGHTS_ON = "  insights:\n    enabled: true\n"
CLAIM = "The snake plant in the gritty soil mix is still watered like it is summer."


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": word_vector(text))


def _new_page(text: str) -> dict[str, Any]:
    """An insight that names the knowledge page the research run published."""
    found = re.search(r"^## (houseplants/snake-plant/[a-z0-9-]+\.md)$", text, re.MULTILINE)
    assert found, "the worker got no knowledge page"
    return answer(
        item(
            claim=CLAIM,
            we_do=["gritty soil mix"],
            we_do_paths=[SNAKE],
            knowledge_says=[found.group(1)],
            evidence="Our page and the new answer disagree.",
        )
    )


class Workers:
    """The research workers and the insight worker, by prompt."""

    def __init__(self, insight: Any = _new_page) -> None:
        self.research = FakeWorkers(raise_answer=raised((GROUNDED, GROUNDED_PHRASES)))
        self.insight = FakeInsightWorker(insight)

    async def __call__(self, **kwargs: Any) -> dict[str, Any] | None:
        if kwargs["system_prompt"] == INSIGHT_PROMPT:
            return await self.insight(**kwargs)
        return await self.research(**kwargs)


class Harness:
    def __init__(self, data: Path, extra: str = INSIGHTS_ON, insight: Any = _new_page) -> None:
        self.data = data
        self.wiki = build_research_wiki(data)
        self.settings = research_settings(extra)
        self.workers = Workers(insight)

    def job(self) -> ResearchJob:
        return ResearchJob(
            store=FakeStore(),
            settings=self.settings,
            data_dir=self.data,
            runner=self.workers,
            internet_runner=FakeInternet(),
        )

    async def run(self, *, dry_run: bool = False, **args: Any):
        job = self.job()
        args = job.check_args({"mode": "run", "topic": SNAKE, **args})
        return await job.run_once(args=args, dry_run=dry_run, clock=fixed_clock())


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "data")


async def test_a_published_page_runs_the_insight_pass_in_the_same_run(h: Harness) -> None:
    report = await h.run()
    [found] = load_insights(h.data)
    assert found.insight.topic == SNAKE and found.insight.claim == CLAIM
    written = {p.relative_to(h.wiki).as_posix() for p in report.written}
    assert f"insights/{found.id}.md" in written and "insights/README.md" in written
    assert any(p.startswith("knowledge/houseplants/snake-plant/") for p in written)
    insight = report.data["usage"]["insight"]
    assert (insight["calls"], insight["tokens"]) == (1, INSIGHT_USAGE.total)
    assert report.tokens == sum(v["tokens"] for v in report.data["usage"].values())
    sections = dict(report.sections)
    assert "(1 new, 0 refreshed" in sections["Insights"]
    assert "### New" in sections["Insights"] and "### Rejected" in sections["Insights"]
    assert [s[0] for s in report.sections][-2:] == ["Insights", "Usage"]
    assert report.data["insights"]["new"] == [found.id]


async def test_then_insights_false_skips_the_pass(h: Harness) -> None:
    report = await h.run(then_insights="false")
    assert h.workers.insight.calls == [] and load_insights(h.data) == []
    assert "Insights" not in dict(report.sections)
    assert report.data["insights"] is None


async def test_after_research_false_skips_the_pass(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", INSIGHTS_ON + "    after_research: false\n")
    report = await h.run()
    assert report.data["pages"] and h.workers.insight.calls == []


async def test_then_insights_true_runs_when_after_research_is_off(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", INSIGHTS_ON + "    after_research: false\n")
    report = await h.run(then_insights=True)
    assert len(h.workers.insight.calls) == 1
    assert "Insights" in dict(report.sections)


async def test_with_insights_off_the_pass_does_not_run(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", "")
    report = await h.run()
    assert h.workers.insight.calls == [] and "Insights" not in dict(report.sections)
    report = await Harness(tmp_path / "other", "").run(then_insights="yes")
    assert "jobs.insights.enabled is false" in dict(report.sections)["Insights"]


async def test_a_dry_research_run_gives_a_dry_insight_pass(h: Harness) -> None:
    before = snapshot(h.wiki)
    report = await h.run(dry_run=True)
    assert changed(before, snapshot(h.wiki)) == set() and report.written == []
    body = dict(report.sections)["Insights"]
    assert body.startswith("Dry run: insights:") and "(1 new," in body
    assert "### Pages" in body and CLAIM in body
    assert "houseplants/snake-plant/" in h.workers.insight.calls[0]


async def test_an_insight_worker_failure_leaves_the_research_run_good(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", insight=RuntimeError("model down"))
    report = await h.run()
    assert report.error is None
    assert report.data["pages"] and load_insights(h.data) == []
    assert "gave no answer" in dict(report.sections)["Insights"]


async def test_a_failing_pass_is_a_section_not_a_failed_run(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(InsightPass, "run", boom)
    report = await h.run()
    assert report.error is None and report.data["pages"]
    assert dict(report.sections)["Insights"] == "The insight pass failed: disk full"
    assert report.data["insights"] == {"error": "disk full"}


def test_then_insights_arguments(h: Harness) -> None:
    job = h.job()
    assert job.check_args({"mode": "run", "then_insights": "No"})["then_insights"] is False
    assert job.check_args({"mode": "run", "then_insights": True})["then_insights"] is True
    assert "then_insights" not in job.check_args({"mode": "run", "then_insights": ""})
    for bad in ({"mode": "plan", "then_insights": True}, {"mode": "run", "then_insights": "x"}):
        with pytest.raises(JobArgsError):
            job.check_args(bad)
    assert parse_flag("on", "x") is True
    with pytest.raises(JobArgsError):
        parse_flag(1, "x")


def _last_commit(wiki: Path) -> str:
    return subprocess.run(
        ["git", "show", "--name-only", "--format=%s", "HEAD"],
        cwd=wiki,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")
async def test_one_commit_holds_the_research_and_the_insight(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data")
    wikigit.ensure_repo(h.wiki)
    wikigit.commit(h.wiki, None, "start")
    runner = JobRunner(
        repo=FakeJobRunStore(),
        settings=h.settings,
        data_dir=h.data,
        jobs=[h.job()],
        lock=asyncio.Lock(),
        clock=fixed_clock(),
    )
    run_id = await runner.start("research", {"mode": "run", "topic": SNAKE}, dry_run=False)
    await runner.wait(run_id)
    out = _last_commit(h.wiki)
    assert out.startswith(f"research: run {run_id}")
    assert "insights/README.md" in out and "knowledge/houseplants/snake-plant/" in out
    assert layout.insights_index(h.data).is_file()
