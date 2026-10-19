"""The curator through `JobRunner`: it is registered only when it is on, it
tags its findings with the run id, and its run commits every path it wrote."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from curator_fakes import DOT_STRAY, FakeCuratorStore, FakeFindingStore, build_curator_wiki
from jobs_fakes import FakeJobRunStore, fixed_clock, make_settings
from joshua_core.jobs import Job, RunReport, build_job_runner, current_run_id
from joshua_core.jobs.runner import JobRunner
from joshua_core.memory import embed as embed_module
from joshua_shared import wikigit

ON = "jobs:\n  enabled: true\n  curator:\n    enabled: true\n    max_worker_calls: 0\n"


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch):
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [1.0] + [0.0] * 383)


def _git(wiki: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=wiki, capture_output=True, text=True).stdout


def _runner(data: Path, extra: str, findings=None) -> JobRunner:
    return build_job_runner(
        repo=FakeJobRunStore(),
        store=FakeCuratorStore(),
        settings=make_settings(extra),
        data_dir=data,
        lock=asyncio.Lock(),
        clock=fixed_clock(),
        findings=findings,
    )


def test_the_curator_is_registered_only_when_it_is_on(tmp_path: Path) -> None:
    store = FakeFindingStore()
    assert _runner(tmp_path, ON, store).names == ["bundle", "curator"]
    assert _runner(tmp_path, ON, None).names == ["bundle"]
    assert _runner(tmp_path, "jobs:\n  enabled: true\n", store).names == ["bundle"]


class RunIdJob(Job):
    name = "runid"

    def __init__(self) -> None:
        self.seen: int | None = None

    async def run_once(self, *, args, dry_run, clock) -> RunReport:
        self.seen = current_run_id()
        return RunReport(job=self.name, started_at=clock())


async def test_current_run_id_is_the_run_inside_and_none_outside(tmp_path: Path) -> None:
    job = RunIdJob()
    runner = JobRunner(
        repo=FakeJobRunStore(),
        settings=make_settings(),
        data_dir=tmp_path,
        jobs=[job],
        lock=asyncio.Lock(),
        clock=fixed_clock(),
    )
    run_id = await runner.start("runid", {}, dry_run=True)
    await runner.wait(run_id)
    assert job.seen == run_id
    assert current_run_id() is None


@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")
async def test_a_curator_run_commits_what_it_wrote(tmp_path: Path) -> None:
    data = tmp_path / "data"
    wiki = build_curator_wiki(data)
    wikigit.ensure_repo(wiki)
    wikigit.commit(wiki, None, "start")
    findings = FakeFindingStore()
    runner = _runner(data, ON, findings)

    run_id = await runner.start("curator", {}, dry_run=False)
    await runner.wait(run_id)
    row = await runner.get_run(run_id)
    assert row["error"] is None, row["error"]
    assert row["commit_sha"]
    assert _git(wiki, "status", "--porcelain") == ""
    files = _git(wiki, "ls-files")
    assert DOT_STRAY not in files
    assert {r["run_id"] for r in findings.rows.values()} == {run_id}
    assert row["report"]["data"]["counts"]["calls"] == 0
