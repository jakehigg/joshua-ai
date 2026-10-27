"""The `usage_event` methods of `Repo` against a real Postgres: insert, the sum
by local day, and the backfill from `transcript`."""

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
            kind="internet",
            name="extra",
            worker="lookup",
            model="m-lookup",
            usage=Usage(input=7),
            calls=3,
            at=NOON,
            ref="extra:9:lookup",
        )
    )
    await repo.add_usage_event(
        UsageEvent(
            kind="internet",
            name="extra",
            worker="editor",
            model="m-editor",
            usage=Usage(output=2),
            at=NOON,
            ref="extra:9:editor",
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
    assert body["rows"][0]["groups"]["extra"]["workers"]["lookup"]["input"] == 7
    assert body["rows"][0]["groups"]["extra"]["calls"] == 4
    assert body["totals"]["extra"]["models"] == ["m-editor", "m-lookup"]
    assert body["totals"]["extra"]["workers"]["lookup"]["models"] == ["m-lookup"]
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

    first = await repo.backfill_usage()
    second = await repo.backfill_usage()

    assert first == {"chat": 1}
    assert second == {"chat": 0}
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
