"""The job base, the run report, and the runner.

A `Job` does one kind of work on the wiki. `JobRunner.start` runs one job in
the background, under the lock that the nightly reflection also takes, so two
jobs, or a job and the nightly run, never write the wiki at the same time.

Each run has one row in the `job_run` table. When the run is not a dry run,
the runner writes the report as a journal entry at
`wiki/journal/YYYY/MM/DD/<job>-<YYYYMMDD>-<run id>.md` and commits that entry
and every path the job wrote, in one wiki commit. A dry run writes nothing and
records the report in its row only. A run that fails records its error and
never raises out of its task.
"""

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import yaml
from joshua_shared import layout, wikigit
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

logger = get_logger("jobs.runner")

Clock = Callable[[], datetime]

# A job name is part of a journal slug, so it is a short slug itself.
JOB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}\Z")


def utc_now() -> datetime:
    """The current time in UTC. The runner's clock unless a test passes one."""
    return datetime.now(UTC)


class JobError(Exception):
    """A run cannot start."""


class UnknownJob(JobError):
    """No job has that name."""


class JobBusy(JobError):
    """Another job or the nightly reflection holds the lock."""


class JobArgsError(JobError):
    """The arguments of a run are not valid for the job."""


@dataclass
class RunReport:
    """What one run did. It becomes the journal entry of the run.

    `summary` is one paragraph. `sections` is the body of the entry: a list of
    `(heading, markdown)`. `calls` and `tokens` count the worker calls of the
    run. `written` lists every wiki path the run wrote, which the runner
    commits. `data` holds values for a program, such as the bundle paths; it
    goes into the `job_run` row and not into the journal entry.
    """

    job: str
    started_at: datetime
    finished_at: datetime | None = None
    dry_run: bool = True
    args: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    sections: list[tuple[str, str]] = field(default_factory=list)
    calls: int = 0
    tokens: int = 0
    commit_sha: str | None = None
    error: str | None = None
    written: list[Path] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def to_markdown(self) -> str:
        """The body of the journal entry of this run."""
        lines = [f"# {self.job} run", ""]
        if self.summary:
            lines += [self.summary.strip(), ""]
        for heading, body in self.sections:
            lines += [f"## {heading}", "", body.strip() or "None.", ""]
        if self.error:
            lines += ["## Error", "", self.error.strip(), ""]
        args = json.dumps(self.args, sort_keys=True, ensure_ascii=False)
        lines += [
            "## Run",
            "",
            f"- Started: {self.started_at.isoformat()}",
            f"- Finished: {self.finished_at.isoformat() if self.finished_at else 'not yet'}",
            f"- Dry run: {'yes' if self.dry_run else 'no'}",
            f"- Arguments: `{args}`",
            f"- Worker calls: {self.calls}",
            f"- Tokens: {self.tokens}",
            "",
        ]
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        """The report as JSON, for the `report` column of `job_run`."""
        return {
            "job": self.job,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "dry_run": self.dry_run,
            "args": self.args,
            "summary": self.summary,
            "sections": [[heading, body] for heading, body in self.sections],
            "calls": self.calls,
            "tokens": self.tokens,
            "commit_sha": self.commit_sha,
            "error": self.error,
            "written": [str(p) for p in self.written],
            "data": self.data,
        }


class Job(ABC):
    """One kind of work on the wiki. A subclass sets `name` and `run_once`.

    `check_args` runs before the run starts, so a bad argument is an error to
    the caller and not a failed run. A job never writes in a dry run, and it
    lists each path it writes in `RunReport.written`.
    """

    name: str = ""

    def check_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return the arguments to run with, or raise `JobArgsError`."""
        return dict(args)

    @abstractmethod
    async def run_once(self, *, args: dict[str, Any], dry_run: bool, clock: Clock) -> RunReport:
        """Do the work once and return its report."""


class JobRunStore(Protocol):
    """The `job_run` methods of `Repo` that the runner uses."""

    async def create_job_run(
        self, *, job: str, args: dict[str, Any], dry_run: bool, started_at: datetime
    ) -> int: ...

    async def finish_job_run(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        report: dict[str, Any],
        commit_sha: str | None,
        calls: int,
        tokens: int,
        error: str | None,
    ) -> None: ...

    async def get_job_run(self, run_id: int) -> dict[str, Any] | None: ...

    async def list_job_runs(self, job: str | None, limit: int = 20) -> list[dict[str, Any]]: ...


def journal_slug(job: str, day: date, run_id: int) -> str:
    """`<job>-<YYYYMMDD>-<run id>`, the journal slug of one run."""
    return f"{job}-{day:%Y%m%d}-{run_id}"


def run_row_json(row: dict[str, Any]) -> dict[str, Any]:
    """A `job_run` row with each time as an ISO string."""
    out = dict(row)
    for key in ("started_at", "finished_at"):
        value = out.get(key)
        if isinstance(value, datetime):
            out[key] = value.isoformat()
    return out


class JobRunner:
    """Hold the jobs by name and run one at a time.

    `lock` is shared with the nightly reflection. `start` takes it before it
    returns, and the run releases it when it ends.
    """

    def __init__(
        self,
        *,
        repo: JobRunStore,
        settings: JoshuaConfig,
        data_dir: Path | str,
        jobs: Iterable[Job],
        lock: asyncio.Lock,
        clock: Clock | None = None,
    ) -> None:
        self._repo = repo
        self._settings = settings
        self._data_dir = Path(data_dir)
        self._lock = lock
        self._clock = clock or utc_now
        self._jobs: dict[str, Job] = {}
        for job in jobs:
            if not JOB_NAME_RE.match(job.name):
                raise ValueError(f"invalid job name: {job.name!r}")
            if job.name in self._jobs:
                raise ValueError(f"duplicate job name: {job.name!r}")
            self._jobs[job.name] = job
        self._tasks: dict[int, asyncio.Task[None]] = {}

    @property
    def names(self) -> list[str]:
        return sorted(self._jobs)

    @property
    def busy(self) -> bool:
        """True while a job or the nightly reflection holds the lock."""
        return self._lock.locked()

    def has(self, name: str) -> bool:
        return name in self._jobs

    async def start(self, job: str, args: dict[str, Any], dry_run: bool) -> int:
        """Start a run of `job` in the background and return its run id.

        Raises `UnknownJob` for a name with no job, `JobArgsError` for bad
        arguments, and `JobBusy` when the lock is held.
        """
        target = self._jobs.get(job)
        if target is None:
            raise UnknownJob(job)
        checked = target.check_args(dict(args))
        if self._lock.locked():
            raise JobBusy(job)
        await self._lock.acquire()
        try:
            started = self._clock()
            run_id = await self._repo.create_job_run(
                job=job, args=checked, dry_run=dry_run, started_at=started
            )
        except BaseException:
            self._lock.release()
            raise
        task = asyncio.create_task(
            self._run(run_id, target, checked, dry_run, started), name=f"job-{job}-{run_id}"
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda _t, rid=run_id: self._tasks.pop(rid, None))
        logger.info(
            {"message": "job run started", "job": job, "run_id": run_id, "dry_run": dry_run}
        )
        return run_id

    async def wait(self, run_id: int) -> None:
        """Wait for a run of this process to end. A run that ended returns at once."""
        task = self._tasks.get(run_id)
        if task is not None:
            await asyncio.shield(task)

    async def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = await self._repo.get_job_run(run_id)
        return run_row_json(row) if row is not None else None

    async def list_runs(self, job: str | None, limit: int) -> list[dict[str, Any]]:
        return [run_row_json(r) for r in await self._repo.list_job_runs(job, limit)]

    async def last_run(self, job: str) -> dict[str, Any] | None:
        rows = await self._repo.list_job_runs(job, 1)
        return run_row_json(rows[0]) if rows else None

    async def _run(
        self, run_id: int, job: Job, args: dict[str, Any], dry_run: bool, started: datetime
    ) -> None:
        try:
            report = await self._execute(run_id, job, args, dry_run, started)
            await self._finish(run_id, report)
        except Exception as exc:  # noqa: BLE001 — a run must never raise out of its task
            logger.error({"message": "job run not recorded", "run_id": run_id, "error": str(exc)})
        finally:
            self._lock.release()

    async def _execute(
        self, run_id: int, job: Job, args: dict[str, Any], dry_run: bool, started: datetime
    ) -> RunReport:
        try:
            report = await job.run_once(args=args, dry_run=dry_run, clock=self._clock)
        except Exception as exc:  # noqa: BLE001 — the error goes into the report
            report = RunReport(
                job=job.name,
                started_at=started,
                dry_run=dry_run,
                args=args,
                summary="The run failed.",
                error=str(exc) or repr(exc),
            )
        report.job = job.name
        report.started_at = started
        report.dry_run = dry_run
        report.args = args
        report.finished_at = self._clock()
        if not dry_run:
            try:
                report.commit_sha = self._record(run_id, report)
            except Exception as exc:  # noqa: BLE001 — the error goes into the report
                note = f"the journal entry or the commit failed: {exc}"
                report.error = f"{report.error}; {note}" if report.error else note
        return report

    def _record(self, run_id: int, report: RunReport) -> str | None:
        """Write the journal entry of a run, commit it with what the run wrote,
        and return the commit SHA, or None when nothing was committed."""
        assert report.finished_at is not None
        day = report.finished_at.astimezone(ZoneInfo(self._settings.timezone)).date()
        path = layout.journal_entry_path(day, journal_slug(report.job, day, run_id), self._data_dir)
        meta = {"date": day, "people": [], "source": report.job, "run_id": run_id}
        block = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{block}---\n\n{report.to_markdown()}", encoding="utf-8")
        report.written.append(path)
        if not wikigit.is_enabled(self._settings):
            return None
        wiki = layout.wiki_root(self._data_dir)
        if not wikigit.commit(wiki, report.written, f"{report.job}: run {run_id}"):
            return None
        return wikigit.head_sha(wiki)

    async def _finish(self, run_id: int, report: RunReport) -> None:
        assert report.finished_at is not None
        await self._repo.finish_job_run(
            run_id,
            finished_at=report.finished_at,
            report=report.to_json(),
            commit_sha=report.commit_sha,
            calls=report.calls,
            tokens=report.tokens,
            error=report.error,
        )
        logger.info(
            {
                "message": "job run finished",
                "job": report.job,
                "run_id": run_id,
                "dry_run": report.dry_run,
                "calls": report.calls,
                "tokens": report.tokens,
                "commit_sha": report.commit_sha,
                "error": report.error,
            }
        )
