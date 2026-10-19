"""Jobs: work that core does on the wiki when an operator starts it.

Nothing schedules a job. An operator starts a run through
`POST /admin/jobs/{job}/run`, and `JobRunner` runs it in the background under
one lock, records it in the `job_run` table, and, when the run is not a dry
run, writes its report as a journal entry and commits the wiki. See
`docs/jobs.md`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from joshua_shared.config import JoshuaConfig

from joshua_core.jobs.bundle import Bundle, BundleStore, build_bundle
from joshua_core.jobs.bundle_job import BundleJob
from joshua_core.jobs.curator import CuratorJob, CuratorStore, FindingStore
from joshua_core.jobs.insights import InsightsJob
from joshua_core.jobs.research import ResearchJob
from joshua_core.jobs.runner import (
    Clock,
    Job,
    JobArgsError,
    JobBusy,
    JobError,
    JobRunner,
    JobRunStore,
    RunReport,
    UnknownJob,
    current_run_id,
)
from joshua_core.jobs.topics import Topic, is_topic_page, list_topics

__all__ = [
    "Bundle",
    "BundleJob",
    "BundleStore",
    "Clock",
    "CuratorJob",
    "InsightsJob",
    "Job",
    "JobArgsError",
    "JobBusy",
    "JobError",
    "JobRunStore",
    "JobRunner",
    "ResearchJob",
    "RunReport",
    "Topic",
    "UnknownJob",
    "build_bundle",
    "build_job_runner",
    "current_run_id",
    "is_topic_page",
    "list_topics",
]


def build_job_runner(
    *,
    repo: JobRunStore,
    store: BundleStore,
    settings: JoshuaConfig,
    data_dir: Path | str,
    lock: asyncio.Lock,
    clock: Clock | None = None,
    findings: FindingStore | None = None,
) -> JobRunner:
    """The runner with every built-in job. `lock` is the nightly reflection's lock.
    `clock` defaults to the current UTC time.

    The `curator` job is added when `jobs.curator.enabled` is true and
    `findings` holds the `curator_finding` table; `store` must then also
    answer `kb_mean_vectors`. The `research` job is added when
    `jobs.research.enabled` is true.
    """
    jobs: list[Job] = [BundleJob(store=store, settings=settings, data_dir=data_dir)]
    if settings.jobs.curator.enabled and findings is not None:
        jobs.append(
            CuratorJob(
                store=cast(CuratorStore, store),
                findings=findings,
                settings=settings,
                data_dir=data_dir,
            )
        )
    if settings.jobs.research.enabled:
        jobs.append(ResearchJob(store=store, settings=settings, data_dir=data_dir))
    if settings.jobs.insights.enabled:
        jobs.append(InsightsJob(store=store, settings=settings, data_dir=data_dir))
    return JobRunner(
        repo=repo, settings=settings, data_dir=data_dir, jobs=jobs, lock=lock, clock=clock
    )
