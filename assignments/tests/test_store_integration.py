"""``PgStore`` against a real Postgres: the schema, every method, and a restart.

Each test drops the ``assignments`` schema first, so it starts from nothing.
It needs ``DATABASE_URL``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from joshua_assignments.store import PgStore

pytestmark = pytest.mark.integration

NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


@pytest.fixture
async def store():
    url = os.environ["DATABASE_URL"]
    fresh = PgStore(url)
    await fresh.open()
    async with fresh.pool.connection() as conn:
        await conn.execute("DROP SCHEMA assignments CASCADE")
    await fresh.close()
    opened = PgStore(url)
    await opened.open()
    try:
        yield opened
    finally:
        await opened.close()


async def create(store: PgStore, person: str = "alex") -> dict:
    return await store.create(
        person_id=person,
        conversation_id="conv-1",
        spec={"goal": "Audit", "budget": {"five_hour_pct": 70}},
        model="claude-sonnet-5",
        page_path=lambda i: f"assignments/{i}-audit.md",
    )


async def test_create_get_and_a_restart_finds_the_rows(store) -> None:
    row = await create(store)
    assert row["status"] == "queued"
    assert row["page_path"] == f"assignments/{row['id']}-audit.md"
    assert row["spec"]["budget"] == {"five_hour_pct": 70}
    await store.add_checkpoint(row["id"], "plan written")
    await store.close()

    again = PgStore(os.environ["DATABASE_URL"])
    await again.open()
    try:
        found = await again.get(row["id"])
        checkpoint = await again.last_checkpoint(row["id"])
    finally:
        await again.close()
    assert found is not None and found["spec"] == row["spec"]
    assert found["conversation_id"] == "conv-1"
    assert checkpoint["note"] == "plan written"
    await store.open()


async def test_update_list_counts_and_the_queue(store) -> None:
    a = await create(store)
    b = await create(store)
    c = await create(store, person="mia")
    assert (await store.next_queued())["id"] == a["id"]
    updated = await store.update(
        a["id"],
        status="running",
        started_at=NOW,
        follow_ups=[{"title": "x"}],
        stopped_at_limits=None,
        input=5,
    )
    assert updated["status"] == "running" and updated["follow_ups"] == [{"title": "x"}]
    assert updated["started_at"] == NOW and updated["input"] == 5
    assert (await store.update(a["id"]))["id"] == a["id"]
    assert await store.update(9999, status="done") is None
    assert await store.get(9999) is None
    assert [r["id"] for r in await store.recent("alex", 10)] == [b["id"], a["id"]]
    assert [r["id"] for r in await store.recent(None, 2)] == [c["id"], b["id"]]
    assert [r["id"] for r in await store.recent("alex", 10, "running")] == [a["id"]]
    assert await store.counts() == {"running": 1, "queued": 2}
    assert await store.requeue_running() == [a["id"]]
    assert await store.counts() == {"queued": 3}
    assert await store.last_checkpoint(a["id"]) is None
    assert await store.ping() is True
    with pytest.raises(ValueError):
        await store.update(a["id"], status="finished")


async def test_the_schema_applies_twice(store) -> None:
    await create(store)
    await store.close()
    await store.open()
    assert len(await store.recent(None, 10)) == 1


async def test_slices_count_up_and_a_repeat_is_not_stored(store) -> None:
    row = await create(store)
    assert await store.next_slice_n(row["id"]) == 1
    fields = {
        "usage": {"input": 10, "output": 5, "cache_read": 0, "cache_write": 1},
        "limits_before": {"five_hour": {"used_pct": 20.0, "resets_at": None}},
        "started_at": NOW,
        "ended_at": NOW,
        "reason": "end",
    }
    await store.add_slice(row["id"], n=1, **fields)
    await store.add_slice(row["id"], n=1, **{**fields, "reason": "again"})
    await store.add_slice(row["id"], n=2, **{**fields, "limits_before": None})
    assert await store.next_slice_n(row["id"]) == 3
    async with store.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT n, input, reason, limits_before FROM assignments.slice "
            "WHERE assignment_id = %s ORDER BY n",
            (row["id"],),
        )
        rows = await cur.fetchall()
    assert [(r["n"], r["input"], r["reason"]) for r in rows] == [(1, 10, "end"), (2, 10, "end")]
    assert rows[0]["limits_before"]["five_hour"]["used_pct"] == 20.0
    assert rows[1]["limits_before"] is None
