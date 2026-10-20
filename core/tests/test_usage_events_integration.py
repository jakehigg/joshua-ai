"""The `usage_event` methods of `Repo` against a real Postgres: insert, the sum
by local day, and the backfill from `transcript` and `job_run`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from joshua_core.engine.usage import Usage
from joshua_core.store.usage_events import UsageEvent, summarize

pytestmark = pytest.mark.integration

TZ = "America/New_York"
# 02:00 UTC on the 20th is 22:00 on the 19th in New York.
LATE = datetime(2026, 10, 20, 2, 0, tzinfo=UTC)
NOON = datetime(2026, 10, 20, 16, 0, tzinfo=UTC)
SINCE = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)


async def test_insert_and_sum_by_local_day(repo) -> None:
    await repo.add_usage_event(
        UsageEvent(kind="chat", name="chat", usage=Usage(input=10, output=2), at=LATE, ref="a")
    )
    await repo.add_usage_event(
        UsageEvent(kind="chat", name="chat", usage=Usage(cache_read=5), at=NOON, ref="b")
    )
    await repo.add_usage_event(
        UsageEvent(
            kind="job",
            name="research",
            worker="lookup",
            model="m-lookup",
            usage=Usage(input=7),
            calls=3,
            run_id=9,
            at=NOON,
            ref="job_run:9:lookup",
        )
    )
    await repo.add_usage_event(
        UsageEvent(
            kind="job",
            name="research",
            worker="editor",
            model="m-editor",
            usage=Usage(output=2),
            run_id=9,
            at=NOON,
            ref="job_run:9:editor",
        )
    )
    # The same ref again is skipped.
    await repo.add_usage_event(
        UsageEvent(kind="chat", name="chat", usage=Usage(input=999), at=NOON, ref="b")
    )

    rows = await repo.usage_rows(SINCE, TZ)
    body = summarize(rows, timezone=TZ, days=30).model_dump()

    assert [r["date"] for r in body["rows"]] == ["2026-10-20", "2026-10-19"]
    assert body["rows"][1]["groups"]["chat"]["total"] == 12
    assert body["rows"][0]["groups"]["chat"]["total"] == 5
    assert body["rows"][0]["groups"]["research"]["workers"]["lookup"]["input"] == 7
    assert body["rows"][0]["groups"]["research"]["calls"] == 4
    assert body["totals"]["research"]["models"] == ["m-editor", "m-lookup"]
    assert body["totals"]["research"]["workers"]["lookup"]["models"] == ["m-lookup"]
    assert body["totals"]["chat"]["models"] == []
    # A row before the window is not counted.
    assert await repo.usage_rows(datetime(2026, 10, 21, tzinfo=UTC), TZ) == []


async def test_backfill_reads_old_rows_once(repo) -> None:
    await repo.upsert_channel("telegram:1", "telegram", session_mode="per_person")
    await repo.upsert_person("alex", "Alex")
    conversation = await repo.get_or_create_conversation("telegram:1", "alex")
    sdk = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 300,
        "cache_creation_input_tokens": 4,
    }
    await repo.add_transcript(conversation.id, "in", "hi", meta={"turn_id": "t"})
    await repo.add_transcript(conversation.id, "out", "hello", meta={"usage": sdk})
    await repo.add_transcript(conversation.id, "out", "no usage", meta={"usage": None})

    old = await repo.create_job_run(job="curator", args={}, dry_run=True, started_at=NOON)
    await repo.finish_job_run(
        old,
        finished_at=NOON,
        report={"data": {"usage": {"review": {"calls": 2, "tokens": 500}}}},
        commit_sha=None,
        calls=2,
        tokens=500,
        error=None,
    )
    split = {"calls": 1, "tokens": 9, "input": 4, "output": 5, "cache_read": 0, "cache_write": 0}
    new = await repo.create_job_run(job="research", args={}, dry_run=False, started_at=NOON)
    await repo.finish_job_run(
        new,
        finished_at=NOON,
        report={"data": {"usage": {"editor": split}}},
        commit_sha=None,
        calls=1,
        tokens=9,
        error=None,
    )
    await repo.create_job_run(job="insights", args={}, dry_run=True, started_at=NOON)

    first = await repo.backfill_usage()
    second = await repo.backfill_usage()

    assert first == {"chat": 1, "job": 2, "reflection": 0}
    assert second == {"chat": 0, "job": 0, "reflection": 0}
    async with repo._pool.connection() as conn:  # noqa: SLF001 — read the rows back
        cur = await conn.execute("SELECT * FROM usage_event ORDER BY ref")
        rows = {r["ref"]: r for r in await cur.fetchall()}
    chat = next(r for ref, r in rows.items() if ref.startswith("transcript:"))
    assert (chat["kind"], chat["person_id"], chat["model"]) == ("chat", "alex", None)
    assert (chat["input"], chat["output"], chat["cache_read"], chat["cache_write"]) == (
        100,
        20,
        300,
        4,
    )
    review = rows[f"job_run:{old}:review"]
    assert (review["calls"], review["input"], review["output"]) == (2, 500, 0)
    editor = rows[f"job_run:{new}:editor"]
    assert (editor["input"], editor["output"], editor["worker"]) == (4, 5, "editor")


async def test_the_backfill_renames_the_nightly_rows_once(repo) -> None:
    for ref, kind in (("old-1", "nightly"), ("old-2", "nightly"), ("new-1", "reflection")):
        await repo.add_usage_event(
            UsageEvent(kind=kind, name=kind, usage=Usage(input=3), model="m", at=NOON, ref=ref)
        )

    first = await repo.backfill_usage()
    second = await repo.backfill_usage()

    assert first["reflection"] == 2
    assert second["reflection"] == 0
    async with repo._pool.connection() as conn:  # noqa: SLF001 — read the rows back
        cur = await conn.execute("SELECT ref, kind, name, input FROM usage_event ORDER BY ref")
        rows = await cur.fetchall()
    assert [(r["ref"], r["kind"], r["name"], r["input"]) for r in rows] == [
        ("new-1", "reflection", "reflection", 3),
        ("old-1", "reflection", "reflection", 3),
        ("old-2", "reflection", "reflection", 3),
    ]
    body = summarize(await repo.usage_rows(SINCE, TZ), timezone=TZ, days=30).model_dump()
    assert body["groups"] == ["reflection"]
    assert body["totals"]["reflection"]["calls"] == 3
    assert body["totals"]["reflection"]["models"] == ["m"]
