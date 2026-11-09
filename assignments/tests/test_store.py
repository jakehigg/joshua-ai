"""The store rules that need no database: the columns, the statuses, the schema, and concurrency."""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from typing import Any

import anyio
import pytest
from joshua_assignments.store import (
    FINISHED,
    PARKED,
    SCHEMA_SQL,
    STATUSES,
    UPDATABLE,
    PgStore,
    check_fields,
)


def test_the_statuses_are_the_nine_of_the_plan() -> None:
    assert STATUSES == (
        "draft",
        "running",
        "waiting",
        "paused_limits",
        "done",
        "failed",
        "cancelled",
        "budget_exceeded",
        "time_exceeded",
    )
    assert PARKED | FINISHED < set(STATUSES)
    assert not PARKED & FINISHED


def test_check_fields_refuses_an_unknown_column_and_status() -> None:
    check_fields({"status": "done", "summary": "x"})
    with pytest.raises(ValueError):
        check_fields({"person_id": "mia"})
    with pytest.raises(ValueError):
        check_fields({"status": "finished"})
    assert "person_id" not in UPDATABLE and "id" not in UPDATABLE


def test_the_schema_is_additive_only() -> None:
    upper = SCHEMA_SQL.upper()
    for word in ("DROP ", "ALTER TABLE ASSIGNMENTS.ASSIGNMENT DROP", "TRUNCATE", "RENAME"):
        assert word not in upper
    for statement in re.findall(r"CREATE (?:TABLE|INDEX|SCHEMA)[^\n]*", SCHEMA_SQL):
        assert "IF NOT EXISTS" in statement
    assert "CREATE SCHEMA IF NOT EXISTS assignments;" in SCHEMA_SQL


def test_the_schema_has_every_column_of_the_plan() -> None:
    for column in (
        "person_id",
        "conversation_id",
        "spec",
        "status",
        "model",
        "session_id",
        "page_path",
        "input",
        "output",
        "cache_read",
        "cache_write",
        "turns",
        "started_at",
        "finished_at",
        "working_s",
        "stopped_at_limits",
        "open_question",
        "asked_at",
        "answer",
        "summary",
        "follow_ups",
        "error",
        "limits_before",
        "ended_at",
        "reason",
    ):
        assert re.search(rf"\n\s+{column}\s", SCHEMA_SQL), column


def test_the_schema_adds_the_plan_the_report_and_the_outputs() -> None:
    for column, kind in (
        ("plan", "TEXT"),
        ("plan_at", "TIMESTAMPTZ"),
        ("report", "TEXT"),
        ("report_at", "TIMESTAMPTZ"),
        ("outputs", "JSONB"),
    ):
        line = f"ALTER TABLE assignments.assignment ADD COLUMN IF NOT EXISTS {column} {kind};"
        assert line in SCHEMA_SQL
    # page_path stays a column, and nothing writes it.
    assert "page_path" not in UPDATABLE and {"plan", "report", "outputs"}.isdisjoint(UPDATABLE)


async def test_a_store_that_is_not_open_refuses() -> None:
    store = PgStore("postgresql://unused")
    with pytest.raises(RuntimeError):
        await store.get(1)
    assert await store.ping() is False
    await store.close()


class BusyError(Exception):
    """What psycopg raises for a second command on a busy connection."""


class FakeCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    async def fetchone(self) -> dict[str, Any] | None:
        return self.row

    async def fetchall(self) -> list[dict[str, Any]]:
        return [self.row] if self.row else []


class FakeConn:
    """One connection: a command waits on the pool gate, and a second command at once fails."""

    def __init__(self, pool: FakePool) -> None:
        self.pool = pool
        self.busy = False

    async def execute(self, query: str, params: Any = None) -> FakeCursor:
        if self.busy:
            raise BusyError("another command is already in progress")
        self.busy = True
        try:
            self.pool.started.set()
            await self.pool.gate.wait()
            if self.pool.fail:
                raise RuntimeError("the database went away")
            self.pool.queries.append(query)
        finally:
            self.busy = False
        return FakeCursor({"id": 1, "n": 1, "status": "queued"})

    @asynccontextmanager
    async def transaction(self):
        yield


class FakePool:
    """A pool of ``size`` connections. A connection comes back only when it is not busy."""

    def __init__(self, size: int = 2) -> None:
        self.free = [FakeConn(self) for _ in range(size)]
        self.gate = asyncio.Event()
        self.gate.set()
        self.started = asyncio.Event()
        self.queries: list[str] = []
        self.returned_busy = 0
        self.fail = False
        self.closed = False
        self._ready = asyncio.Condition()

    @asynccontextmanager
    async def connection(self):
        async with self._ready:
            await self._ready.wait_for(lambda: bool(self.free))
            conn = self.free.pop()
        try:
            yield conn
        finally:
            if conn.busy:
                self.returned_busy += 1
            async with self._ready:
                self.free.append(conn)
                self._ready.notify()

    async def close(self) -> None:
        self.closed = True


def open_store(pool: FakePool) -> PgStore:
    store = PgStore("postgresql://unused")
    store.pool = pool  # type: ignore[assignment]
    return store


async def test_an_anyio_cancel_does_not_stop_the_query() -> None:
    pool = FakePool()
    store = open_store(pool)
    pool.gate.clear()
    with anyio.move_on_after(5) as scope:
        call = asyncio.ensure_future(store.add_checkpoint(1, "plan written"))
        await pool.started.wait()
        scope.cancel()
        await call
    assert scope.cancelled_caught
    assert pool.queries == []
    pool.gate.set()
    await store.close()
    assert [q.split(" (")[0] for q in pool.queries] == ["INSERT INTO assignments.checkpoint"]
    assert pool.returned_busy == 0 and len(pool.free) == 2 and pool.closed


async def test_two_cancels_of_one_caller_do_not_stop_the_query() -> None:
    pool = FakePool()
    store = open_store(pool)
    pool.gate.clear()
    call = asyncio.ensure_future(store.update(1, summary="x"))
    await pool.started.wait()
    call.cancel()
    await asyncio.sleep(0)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    pool.gate.set()
    await store.close()
    assert [q.split(" SET ")[0] for q in pool.queries] == ["UPDATE assignments.assignment"]
    assert pool.returned_busy == 0


async def test_concurrent_calls_never_share_a_connection() -> None:
    pool = FakePool(size=2)
    store = open_store(pool)
    calls = [store.add_checkpoint(1, f"note {i}") for i in range(10)]
    calls += [store.get(1) for _ in range(10)]
    calls += [store.counts() for _ in range(5)]
    results = await asyncio.gather(*calls)
    assert len(pool.queries) == 25 and len(results) == 25
    assert pool.returned_busy == 0 and len(pool.free) == 2
    assert not store._running


async def test_an_error_goes_to_the_caller() -> None:
    pool = FakePool()
    store = open_store(pool)
    with pytest.raises(ValueError):
        await store.update(1, person_id="mia")
    pool.fail = True
    with pytest.raises(RuntimeError):
        await store.get(1)


async def test_the_error_of_a_cancelled_caller_is_logged_by_type(caplog) -> None:
    pool = FakePool()
    store = open_store(pool)
    pool.gate.clear()
    pool.fail = True
    call = asyncio.ensure_future(store.add_checkpoint(1, "a secret note"))
    await pool.started.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    with caplog.at_level("WARNING", logger="joshua_assignments.store"):
        pool.gate.set()
        await store.close()
        await asyncio.sleep(0)
    assert "a store operation failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "a secret note" not in caplog.text


async def test_close_waits_a_bounded_time(monkeypatch) -> None:
    monkeypatch.setattr("joshua_assignments.store.CLOSE_WAIT_S", 0.01)
    pool = FakePool()
    store = open_store(pool)
    pool.gate.clear()
    call = asyncio.ensure_future(store.ping())
    await pool.started.wait()
    await store.close()
    assert pool.closed and store.pool is None
    pool.gate.set()
    assert await call is True
