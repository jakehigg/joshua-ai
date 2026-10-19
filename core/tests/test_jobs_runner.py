"""The job runner: the lock, the run row, the journal entry, and the commit.

The `job_run` table is an in-memory fake. The wiki is a real git repository in
a temporary directory, so the commit and its SHA are real.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jobs_fakes import (
    NOW,
    TOPIC,
    FakeJobRunStore,
    FakeStore,
    build_wiki,
    fixed_clock,
    make_settings,
)
from joshua_core.jobs import (
    BundleJob,
    Job,
    JobArgsError,
    JobBusy,
    JobRunner,
    RunReport,
    UnknownJob,
    build_job_runner,
)
from joshua_core.jobs.bundle_job import BundleJob as BundleJobClass
from joshua_core.jobs.runner import journal_slug, run_row_json, utc_now
from joshua_core.memory import embed as embed_module
from joshua_core.memory.nightly import NightlyReflector
from joshua_shared import frontmatter, wikigit
from nightly_fakes import FakeIndexer, FakeManager, FakeReflectRepo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch):
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [1.0] + [0.0] * 383)


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    wiki = build_wiki(root)
    wikigit.ensure_repo(wiki)
    wikigit.commit(wiki, None, "start")
    return root


class EchoJob(Job):
    """Writes one page unless the run is dry, and reports it."""

    name = "echo"

    def __init__(self, data_root: Path, *, fail: bool = False, gate: asyncio.Event | None = None):
        self._data_root = data_root
        self._fail = fail
        self._gate = gate

    async def run_once(self, *, args: dict[str, Any], dry_run: bool, clock) -> RunReport:
        if self._gate is not None:
            await self._gate.wait()
        if self._fail:
            raise RuntimeError("the worker broke")
        report = RunReport(
            job=self.name,
            started_at=clock(),
            summary="Echoed.",
            sections=[("Said", str(args.get("say", "")))],
            calls=2,
            tokens=150,
        )
        if not dry_run:
            page = self._data_root / "wiki" / "echo.md"
            page.write_text(f"{args.get('say', '')}\n")
            report.written.append(page)
        return report


def _runner(data_root: Path, *jobs: Job, extra: str = "", lock=None, repo=None) -> JobRunner:
    return JobRunner(
        repo=repo or FakeJobRunStore(),
        settings=make_settings(extra),
        data_dir=data_root,
        jobs=jobs,
        lock=lock or asyncio.Lock(),
        clock=fixed_clock(),
    )


def _git(wiki: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=wiki, capture_output=True, text=True, check=True
    ).stdout.strip()


ENTRY_DIR = ("wiki", "journal", "2026", "10", "19")


async def test_a_real_run_writes_the_entry_and_commits_once(data_root: Path) -> None:
    repo = FakeJobRunStore()
    runner = _runner(data_root, EchoJob(data_root), repo=repo)
    run_id = await runner.start("echo", {"say": "hello"}, dry_run=False)
    await runner.wait(run_id)

    entry = data_root.joinpath(*ENTRY_DIR, f"echo-20261019-{run_id}.md")
    meta, body = frontmatter.split(entry.read_text())
    assert meta == {
        "date": NOW.date(),
        "people": [],
        "source": "echo",
        "run_id": run_id,
    }
    assert "# echo run" in body and "Echoed." in body and "## Said" in body
    assert "- Worker calls: 2" in body and "- Tokens: 150" in body

    wiki = data_root / "wiki"
    row = repo.rows[run_id]
    assert row["commit_sha"] == _git(wiki, "rev-parse", "HEAD")
    assert _git(wiki, "log", "-1", "--format=%s") == f"echo: run {run_id}"
    changed = _git(wiki, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert sorted(changed) == sorted(["echo.md", f"journal/2026/10/19/echo-20261019-{run_id}.md"])
    assert row["calls"] == 2 and row["tokens"] == 150 and row["error"] is None
    assert row["finished_at"] == NOW
    assert row["report"]["summary"] == "Echoed."
    assert row["report"]["commit_sha"] == row["commit_sha"]
    assert runner.busy is False


async def test_a_dry_run_writes_nothing(data_root: Path) -> None:
    repo = FakeJobRunStore()
    runner = _runner(data_root, EchoJob(data_root), repo=repo)
    head = _git(data_root / "wiki", "rev-parse", "HEAD")
    run_id = await runner.start("echo", {"say": "hello"}, dry_run=True)
    await runner.wait(run_id)
    assert not (data_root / "wiki" / "echo.md").exists()
    assert not data_root.joinpath(*ENTRY_DIR).exists()
    assert _git(data_root / "wiki", "rev-parse", "HEAD") == head
    row = repo.rows[run_id]
    assert row["dry_run"] is True and row["commit_sha"] is None
    assert row["report"]["dry_run"] is True
    assert row["report"]["sections"] == [["Said", "hello"]]


async def test_a_failed_run_records_the_error_and_releases_the_lock(data_root: Path) -> None:
    repo = FakeJobRunStore()
    runner = _runner(data_root, EchoJob(data_root, fail=True), repo=repo)
    run_id = await runner.start("echo", {}, dry_run=False)
    await runner.wait(run_id)
    row = repo.rows[run_id]
    assert row["error"] == "the worker broke"
    assert runner.busy is False
    entry = data_root.joinpath(*ENTRY_DIR, f"echo-20261019-{run_id}.md")
    assert "## Error" in entry.read_text() and "the worker broke" in entry.read_text()


async def test_a_failed_dry_run_records_the_error_only(data_root: Path) -> None:
    repo = FakeJobRunStore()
    runner = _runner(data_root, EchoJob(data_root, fail=True), repo=repo)
    run_id = await runner.start("echo", {}, dry_run=True)
    await runner.wait(run_id)
    assert repo.rows[run_id]["error"] == "the worker broke"
    assert not data_root.joinpath(*ENTRY_DIR).exists()


async def test_a_second_start_while_a_run_holds_the_lock_is_busy(data_root: Path) -> None:
    gate = asyncio.Event()
    repo = FakeJobRunStore()
    runner = _runner(data_root, EchoJob(data_root, gate=gate), repo=repo)
    first = await runner.start("echo", {}, dry_run=True)
    assert runner.busy is True
    with pytest.raises(JobBusy):
        await runner.start("echo", {}, dry_run=True)
    assert list(repo.rows) == [first]
    gate.set()
    await runner.wait(first)
    assert runner.busy is False
    second = await runner.start("echo", {}, dry_run=True)
    await runner.wait(second)
    assert repo.rows[second]["finished_at"] is not None


async def test_the_nightly_reflection_shares_the_lock(data_root: Path) -> None:
    lock = asyncio.Lock()
    gate = asyncio.Event()
    runner = _runner(data_root, EchoJob(data_root, gate=gate), lock=lock)
    reflector = NightlyReflector(
        repo=FakeReflectRepo(people=[]),
        indexer=FakeIndexer(),
        manager=FakeManager(),
        settings=make_settings(),
        data_dir=data_root,
        lock=lock,
    )
    run_id = await runner.start("echo", {}, dry_run=True)
    reflect = asyncio.create_task(reflector.reflect())
    await asyncio.sleep(0.05)
    # The reflection waits for the job.
    assert not reflect.done()
    gate.set()
    await runner.wait(run_id)
    summary = await asyncio.wait_for(reflect, timeout=5)
    assert summary["errors"] == 0

    # And a job cannot start while the reflection holds the lock.
    async with lock:
        with pytest.raises(JobBusy):
            await runner.start("echo", {}, dry_run=True)


async def test_an_unknown_job_and_bad_arguments_start_nothing(data_root: Path) -> None:
    repo = FakeJobRunStore()
    runner = _runner(
        data_root,
        BundleJob(store=FakeStore(), settings=make_settings(), data_dir=data_root),
        repo=repo,
    )
    with pytest.raises(UnknownJob):
        await runner.start("nope", {}, dry_run=True)
    for args in ({}, {"topic": ""}, {"topic": "houseplants.md"}, {"topic": TOPIC, "x": 1}):
        with pytest.raises(JobArgsError):
            await runner.start("bundle", args, dry_run=True)
    assert repo.rows == {}
    assert runner.busy is False


async def test_without_wiki_git_the_entry_is_written_and_nothing_is_committed(
    data_root: Path,
) -> None:
    repo = FakeJobRunStore()
    runner = _runner(data_root, EchoJob(data_root), repo=repo, extra="wiki:\n  git: false\n")
    head = _git(data_root / "wiki", "rev-parse", "HEAD")
    run_id = await runner.start("echo", {}, dry_run=False)
    await runner.wait(run_id)
    assert data_root.joinpath(*ENTRY_DIR, f"echo-20261019-{run_id}.md").is_file()
    assert repo.rows[run_id]["commit_sha"] is None
    assert _git(data_root / "wiki", "rev-parse", "HEAD") == head


async def test_a_failed_commit_is_recorded_as_an_error(data_root: Path, monkeypatch) -> None:
    def _boom(wiki, paths, message):
        raise ValueError("path outside the wiki")

    monkeypatch.setattr(wikigit, "commit", _boom)
    repo = FakeJobRunStore()
    runner = _runner(data_root, EchoJob(data_root, fail=True), repo=repo)
    run_id = await runner.start("echo", {}, dry_run=False)
    await runner.wait(run_id)
    error = repo.rows[run_id]["error"]
    assert error.startswith("the worker broke; the journal entry or the commit failed")


async def test_a_commit_with_nothing_new_stores_no_sha(data_root: Path, monkeypatch) -> None:
    monkeypatch.setattr(wikigit, "commit", lambda wiki, paths, message: False)
    repo = FakeJobRunStore()
    runner = _runner(data_root, EchoJob(data_root), repo=repo)
    run_id = await runner.start("echo", {}, dry_run=False)
    await runner.wait(run_id)
    assert repo.rows[run_id]["commit_sha"] is None
    assert repo.rows[run_id]["error"] is None


async def test_a_lost_run_row_never_raises_and_releases_the_lock(data_root: Path) -> None:
    repo = FakeJobRunStore()
    repo.fail_finish = True
    runner = _runner(data_root, EchoJob(data_root), repo=repo)
    run_id = await runner.start("echo", {}, dry_run=True)
    await runner.wait(run_id)
    assert runner.busy is False
    assert repo.rows[run_id]["finished_at"] is None


async def test_a_lost_create_releases_the_lock(data_root: Path) -> None:
    class BrokenStore(FakeJobRunStore):
        async def create_job_run(self, **kwargs):
            raise RuntimeError("database gone")

    runner = _runner(data_root, EchoJob(data_root), repo=BrokenStore())
    with pytest.raises(RuntimeError):
        await runner.start("echo", {}, dry_run=True)
    assert runner.busy is False


async def test_the_bundle_job_reports_the_paths(data_root: Path) -> None:
    repo = FakeJobRunStore()
    store = FakeStore({"wiki/journal/2026/10/08/bread.md": 0.9})
    runner = build_job_runner(
        repo=repo,
        store=store,
        settings=make_settings(),
        data_dir=data_root,
        lock=asyncio.Lock(),
        clock=fixed_clock(),
    )
    assert runner.names == ["bundle"]
    assert runner.has("bundle")
    run_id = await runner.start("bundle", {"topic": f" {TOPIC} "}, dry_run=False)
    await runner.wait(run_id)
    row = repo.rows[run_id]
    assert row["error"] is None
    assert row["args"] == {"topic": TOPIC}
    data = row["report"]["data"]
    assert data["topic"] == TOPIC and data["title"] == "Snake plant"
    assert data["paths"][0] == TOPIC
    assert "journal/2026/10/08/bread.md" in data["paths"]
    assert data["aliases"] == ["Sansevieria", "Snakey"]
    assert data["truncated"] is False
    assert row["report"]["summary"].startswith(f"The bundle of {TOPIC} holds ")
    # The journal body is the path list, not the bundle text.
    entry = data_root.joinpath(*ENTRY_DIR, f"bundle-20261019-{run_id}.md")
    body = frontmatter.split(entry.read_text())[1]
    assert f"- `{TOPIC}`" in body
    assert "Gritty mix" not in body
    assert row["commit_sha"] is not None


async def test_the_bundle_job_reports_a_cut(data_root: Path) -> None:
    repo = FakeJobRunStore()
    job = BundleJob(
        store=FakeStore(),
        settings=make_settings("jobs:\n  bundle:\n    max_chars: 10\n"),
        data_dir=data_root,
    )
    runner = _runner(data_root, job, repo=repo)
    run_id = await runner.start("bundle", {"topic": TOPIC}, dry_run=True)
    await runner.wait(run_id)
    report = repo.rows[run_id]["report"]
    assert report["data"]["truncated"] is True
    assert "cut at jobs.bundle.max_chars" in report["summary"]


async def test_the_runner_reads_runs_back(data_root: Path) -> None:
    runner = _runner(data_root, EchoJob(data_root))
    assert await runner.last_run("echo") is None
    assert await runner.get_run(99) is None
    first = await runner.start("echo", {}, dry_run=True)
    await runner.wait(first)
    second = await runner.start("echo", {}, dry_run=True)
    await runner.wait(second)
    last = await runner.last_run("echo")
    assert last is not None and last["id"] == second
    assert last["started_at"] == NOW.isoformat()
    assert [r["id"] for r in await runner.list_runs("echo", 10)] == [second, first]
    assert [r["id"] for r in await runner.list_runs(None, 1)] == [second]
    got = await runner.get_run(first)
    assert got is not None and got["finished_at"] == NOW.isoformat()
    # A run that ended waits for nothing.
    await runner.wait(first)


def test_job_names_must_be_slugs_and_unique(data_root: Path) -> None:
    class Bad(EchoJob):
        name = "Bad Name"

    with pytest.raises(ValueError):
        _runner(data_root, Bad(data_root))
    with pytest.raises(ValueError):
        _runner(data_root, EchoJob(data_root), EchoJob(data_root))


def test_report_markdown_and_json() -> None:
    started = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    report = RunReport(job="echo", started_at=started, sections=[("Empty", "")])
    text = report.to_markdown()
    assert "## Empty\n\nNone." in text
    assert "- Finished: not yet" in text
    assert "- Dry run: yes" in text
    assert report.to_json()["finished_at"] is None


def test_helpers() -> None:
    assert BundleJobClass is BundleJob
    assert BundleJob.name == "bundle"
    assert journal_slug("bundle", NOW.date(), 7) == "bundle-20261019-7"
    assert run_row_json({"started_at": NOW, "finished_at": None, "id": 1}) == {
        "started_at": NOW.isoformat(),
        "finished_at": None,
        "id": 1,
    }
    assert utc_now().tzinfo is UTC
