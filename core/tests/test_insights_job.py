"""The `insights` job on a fixture wiki: one insight from a summer program and
an autumn knowledge page, no insight without knowledge, the cap, expiry, the
dry run, and the paths it never writes. No model, no network, no database."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from insights_fakes import (
    AUTUMN,
    CLAIM,
    DRAFT,
    INSIGHT_USAGE,
    IRRIGATION,
    LOG,
    LOG_PHRASE,
    PHRASE,
    ROSES,
    FakeInsightWorker,
    answer,
    build_insights_wiki,
    claim_vector,
    insights_settings,
    item,
)
from jobs_fakes import FakeJobRunStore, FakeStore, fixed_clock
from joshua_core.engine import agent
from joshua_core.jobs import JobArgsError, JobRunner, build_job_runner
from joshua_core.jobs.insights.job import InsightsJob, insight_model
from joshua_core.jobs.insights.page import (
    HEADINGS,
    load_insight,
    load_insights,
    set_fields,
)
from joshua_core.memory import embed as embed_module
from joshua_shared import frontmatter, layout
from research_fakes import changed, snapshot


def _split(used: Any) -> dict[str, int]:
    return {
        "input": used.input,
        "output": used.output,
        "cache_read": used.cache_read,
        "cache_write": used.cache_write,
    }


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": claim_vector(text))


class Harness:
    def __init__(self, data: Path, value: Any = None, extra: str = "") -> None:
        self.data = data
        self.wiki = build_insights_wiki(data)
        self.settings = insights_settings(extra)
        self.worker = FakeInsightWorker(value)

    def job(self) -> InsightsJob:
        return InsightsJob(
            store=FakeStore(), settings=self.settings, data_dir=self.data, runner=self.worker
        )

    async def run(self, *, dry_run: bool = False, topic: str = IRRIGATION):
        job = self.job()
        return await job.run_once(
            args=job.check_args({"topic": topic}), dry_run=dry_run, clock=fixed_clock()
        )


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "data")


def _under_insights(paths: list[Path], data: Path) -> bool:
    root = layout.insights_root(data).resolve()
    return all(root in p.resolve().parents for p in paths)


# -- arguments --------------------------------------------------------------------


def test_the_arguments(h: Harness) -> None:
    job = h.job()
    assert job.check_args({"topic": f" {IRRIGATION} "}) == {"topic": IRRIGATION}
    bad_args: list[dict[str, Any]] = [
        {},
        {"topic": ""},
        {"topic": "garden.md"},
        {"topic": LOG},
        {"topic": IRRIGATION, "x": 1},
    ]
    for bad in bad_args:
        with pytest.raises(JobArgsError):
            job.check_args(bad)


def test_the_model_chain() -> None:
    assert insight_model(insights_settings("    model: m1\n")) == "m1"
    assert insight_model(insights_settings("  model: m2\n")) == "m2"
    assert insight_model(insights_settings()) == insights_settings().core.reflection_model


# -- one insight ------------------------------------------------------------------


async def test_a_summer_program_in_autumn_raises_one_insight(h: Harness) -> None:
    report = await h.run()
    assert report.summary == f"insights: {IRRIGATION} (1 new, 0 refreshed, 0 expired, 0 rejected)"
    [found] = load_insights(h.data)
    i = found.insight
    assert found.id.startswith("garden/")
    assert i.topic == IRRIGATION and i.section == "garden" and i.status == "new"
    assert i.claim == CLAIM and i.severity == "act" and i.confidence == "high"
    assert i.knowledge_says == [AUTUMN]
    assert (layout.knowledge_root(h.data) / AUTUMN).is_file()
    bundle_text = (h.wiki / IRRIGATION).read_text()
    assert all(p.lower() in bundle_text.lower() for p in i.we_do)
    assert i.we_do_paths == [IRRIGATION]
    assert i.found_at.isoformat() == "2026-10-19" and i.seen_at == i.found_at
    assert i.told_at is None and "told_at" not in frontmatter.split(found.text)[0]
    assert i.source == "insights"

    body = frontmatter.split(found.text)[1]
    at = [body.index(f"\n## {heading}\n") for heading in HEADINGS]
    assert body.startswith(f"# {CLAIM}\n") and at == sorted(at)
    assert f'"{PHRASE}" in [{IRRIGATION}](../../{IRRIGATION})' in body
    assert "Cut each zone to about half the summer time from October." in body
    assert f"(../../knowledge/{AUTUMN})" in body
    evidence = body.split("## Evidence")[1]
    assert f"[Our page](../../{IRRIGATION})" in evidence
    assert f"[The answer](../../knowledge/{AUTUMN})" in evidence
    assert "evil.example.com" not in evidence and "[[" not in evidence
    assert "\n## A heading" not in body and "A heading the worker planted" in body

    readme = layout.insights_index(h.data).read_text()
    rows = [line for line in readme.splitlines() if line.startswith("| new")]
    assert rows and found.id in rows[0] and "2026-11-30" in rows[0]
    assert set(report.written) == {found.path, layout.insights_index(h.data)}
    assert report.calls == 1 and report.tokens == INSIGHT_USAGE.total
    assert report.data["usage"] == {
        "insight": {"calls": 1, "tokens": INSIGHT_USAGE.total, **_split(INSIGHT_USAGE)}
    }
    assert [s[0] for s in report.sections] == [
        "Knowledge",
        "New",
        "Refreshed",
        "Expired",
        "Rejected",
        "Usage",
    ]


async def test_the_worker_reads_the_bundle_the_knowledge_and_the_day(h: Harness) -> None:
    await h.run()
    [text] = h.worker.calls
    assert "Today: 2026-10-19" in text and "Timezone: America/New_York" in text
    assert "Instance: Test House" in text
    assert f"## {IRRIGATION}" in text and f"## {LOG}" in text
    assert f"## {AUTUMN}" in text and "refresh_after: 2027-03-30" in text
    bundle = text.split("<bundle>")[1]
    assert "## knowledge/" not in bundle
    assert h.worker.kwargs[0]["model"] == insight_model(h.settings)


async def test_a_second_run_refreshes_and_tells_the_worker(h: Harness) -> None:
    await h.run()
    [first] = load_insights(h.data)
    h.worker.value = answer(
        item(
            claim="The garden still runs the 20-minute summer program in late October!",
            we_do=[LOG_PHRASE],
            we_do_paths=[LOG],
        )
    )
    report = await h.run()
    assert "(0 new, 1 refreshed" in report.summary
    [again] = load_insights(h.data)
    assert again.id == first.id and again.insight.status == "new"
    assert again.insight.we_do == [LOG_PHRASE] and again.insight.we_do_paths == [LOG]
    assert "The beds stay wet" in again.text
    assert CLAIM in h.worker.calls[1].split("<open_insights>")[1]


async def test_a_refresh_bumps_seen_at(h: Harness) -> None:
    await h.run()
    [first] = load_insights(h.data)
    set_fields(first, h.data, seen_at=first.insight.found_at.replace(day=1), status="told")
    await h.run()
    [again] = load_insights(h.data)
    assert again.insight.seen_at.isoformat() == "2026-10-19"
    assert again.insight.status == "told"


# -- no knowledge -----------------------------------------------------------------


async def test_a_topic_with_no_knowledge_raises_nothing(h: Harness) -> None:
    before = snapshot(h.wiki)
    report = await h.run(topic=ROSES)
    assert "no knowledge for this topic" in report.summary
    assert report.written == [] and h.worker.calls == []
    assert changed(before, snapshot(h.wiki)) == set()
    assert report.calls == 0


async def test_an_unpublished_page_is_no_knowledge(h: Harness) -> None:
    (layout.knowledge_root(h.data) / AUTUMN).unlink()
    assert (layout.knowledge_root(h.data) / DRAFT).is_file()
    report = await h.run()
    assert "no knowledge for this topic" in report.summary
    assert h.worker.calls == []


# -- the cap and the schema -------------------------------------------------------


async def test_max_per_run_keeps_the_highest_severity(tmp_path: Path) -> None:
    value = answer(
        item(claim="A note about the drip line and the roses bed.", severity="note"),
        item(claim="Frost is near and the summer program still runs.", severity="urgent"),
        item(claim="Mulch would keep the roses moist with less water.", severity="act"),
    )
    h = Harness(tmp_path / "data", value, "    max_per_run: 2\n")
    report = await h.run()
    kept = sorted(f.insight.severity for f in load_insights(h.data))
    assert kept == ["act", "urgent"]
    assert "over jobs.insights.max_per_run (2)" in dict(report.sections)["Rejected"]


async def test_a_worker_answer_that_fails_the_schema_is_a_rejection(tmp_path: Path) -> None:
    value = answer({"claim": "No severity and no confidence in this item."})
    h = Harness(tmp_path / "data", value)
    before = snapshot(h.wiki)
    report = await h.run()
    assert "(0 new, 0 refreshed, 0 expired, 1 rejected)" in report.summary
    assert "does not match the schema" in dict(report.sections)["Rejected"]
    assert changed(before, snapshot(h.wiki)) == set() and report.written == []


async def test_no_answer_changes_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path / "data", RuntimeError("model down"))
    before = snapshot(h.wiki)
    report = await h.run()
    assert "gave no answer" in dict(report.sections)["Notes"]
    assert changed(before, snapshot(h.wiki)) == set()
    assert report.calls == 1


# -- expiry and the dry run -------------------------------------------------------


async def _old_insight(h: Harness) -> str:
    await h.run()
    [found] = load_insights(h.data)
    day = found.insight.found_at
    set_fields(
        found, h.data, valid_from=day.replace(month=9, day=1), valid_until=day.replace(day=18)
    )
    return found.id


async def test_an_open_insight_past_valid_until_expires(h: Harness) -> None:
    old = await _old_insight(h)
    h.worker.value = answer()
    report = await h.run()
    gone = load_insight(old, h.data)
    assert gone is not None and gone.insight.status == "expired"
    assert gone.insight.resolved_by == "expiry"
    assert gone.insight.resolved_at is not None
    assert gone.insight.resolved_at.isoformat() == "2026-10-19"
    assert "1 expired" in report.summary
    assert gone.path in report.written
    readme = layout.insights_index(h.data).read_text()
    assert "| expired | act |" in readme and "| new" not in readme
    assert CLAIM not in h.worker.calls[-1].split("<open_insights>")[1]


async def test_a_dry_run_does_not_expire_and_writes_nothing(h: Harness) -> None:
    old = await _old_insight(h)
    before = snapshot(h.wiki)
    h.worker.value = answer(item(claim="Frost is near and the summer program still runs."))
    report = await h.run(dry_run=True)
    assert changed(before, snapshot(h.wiki)) == set()
    assert report.written == []
    assert load_insight(old, h.data).insight.status == "new"  # type: ignore[union-attr]
    sections = dict(report.sections)
    assert old in sections["Would expire"]
    assert report.summary.startswith("Dry run: insights:")
    assert "---\ntopic: garden/irrigation.md" in sections["Pages"]
    assert "## Why it matters" in sections["Pages"]


async def test_no_path_outside_insights_is_written(h: Harness) -> None:
    report = await h.run()
    assert report.written and _under_insights(report.written, h.data)


# -- the runner -------------------------------------------------------------------


def test_the_agent_still_holds_no_tools() -> None:
    assert agent._NO_TOOLS == []


def test_the_runner_has_the_job_only_when_it_is_on(tmp_path: Path) -> None:
    def runner(settings: Any) -> JobRunner:
        return build_job_runner(
            repo=FakeJobRunStore(),
            store=FakeStore(),
            settings=settings,
            data_dir=tmp_path,
            lock=asyncio.Lock(),
        )

    assert runner(insights_settings()).has("insights")
    off = insights_settings()
    off.jobs.insights.enabled = False
    assert not runner(off).has("insights")
