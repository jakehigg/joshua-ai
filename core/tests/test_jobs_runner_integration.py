"""The `job_run` methods of `Repo` against a real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.integration

STARTED = datetime(2026, 10, 19, 16, 0, tzinfo=UTC)
FINISHED = datetime(2026, 10, 19, 16, 1, tzinfo=UTC)


async def test_job_run_round_trip(repo) -> None:
    first = await repo.create_job_run(
        job="bundle", args={"topic": "garden/roses.md"}, dry_run=True, started_at=STARTED
    )
    second = await repo.create_job_run(job="curator", args={}, dry_run=False, started_at=STARTED)
    assert second > first

    row = await repo.get_job_run(first)
    assert row["job"] == "bundle"
    assert row["args"] == {"topic": "garden/roses.md"}
    assert row["dry_run"] is True
    assert row["finished_at"] is None and row["report"] is None
    assert row["calls"] == 0 and row["tokens"] == 0

    await repo.finish_job_run(
        first,
        finished_at=FINISHED,
        report={"summary": "done", "data": {"paths": ["garden/roses.md"]}},
        commit_sha="abc123",
        calls=3,
        tokens=900,
        error=None,
    )
    row = await repo.get_job_run(first)
    assert row["finished_at"] == FINISHED
    assert row["report"]["data"]["paths"] == ["garden/roses.md"]
    assert row["commit_sha"] == "abc123"
    assert (row["calls"], row["tokens"], row["error"]) == (3, 900, None)

    assert [r["id"] for r in await repo.list_job_runs(None, 10)] == [second, first]
    assert [r["id"] for r in await repo.list_job_runs("bundle", 10)] == [first]
    assert [r["id"] for r in await repo.list_job_runs(None, 1)] == [second]
    assert await repo.get_job_run(9999) is None
