"""The store of the assignments: schema ``assignments`` in the shared Postgres.

One SQL constant, applied at start with ``CREATE SCHEMA IF NOT EXISTS``,
``CREATE TABLE IF NOT EXISTS``, and ``ADD COLUMN IF NOT EXISTS``. It is
additive only: a release never removes a table or a column. The schema keeps
the tables of this container apart from the tables of core in the same
database.

``Store`` is the protocol the manager uses. ``PgStore`` is the Postgres
store.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from joshua_shared.log import get_logger
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

logger = get_logger("joshua_assignments.store")

STATUSES = (
    "queued",
    "running",
    "waiting",
    "paused_limits",
    "done",
    "failed",
    "cancelled",
    "budget_exceeded",
    "time_exceeded",
)
# An assignment that waits for an answer. It holds no session and no slot.
PARKED = frozenset({"waiting", "paused_limits"})
# An assignment that ended. Nothing starts it again.
FINISHED = frozenset({"done", "failed", "cancelled", "budget_exceeded", "time_exceeded"})
USAGE_FIELDS = ("input", "output", "cache_read", "cache_write")
JSON_FIELDS = frozenset({"spec", "stopped_at_limits", "follow_ups"})
# The columns ``update`` may set.
UPDATABLE = frozenset(
    {
        "spec",
        "status",
        "session_id",
        "page_path",
        *USAGE_FIELDS,
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
    }
)

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS assignments;

CREATE TABLE IF NOT EXISTS assignments.assignment (
    id                BIGSERIAL PRIMARY KEY,
    person_id         TEXT NOT NULL,
    conversation_id   TEXT,
    spec              JSONB NOT NULL,
    -- queued, running, waiting, paused_limits, done, failed, cancelled,
    -- budget_exceeded, time_exceeded. The code checks the value, so a new
    -- status needs no change here.
    status            TEXT NOT NULL DEFAULT 'queued',
    model             TEXT NOT NULL,
    session_id        TEXT,
    page_path         TEXT,
    input             BIGINT NOT NULL DEFAULT 0,
    output            BIGINT NOT NULL DEFAULT 0,
    cache_read        BIGINT NOT NULL DEFAULT 0,
    cache_write       BIGINT NOT NULL DEFAULT 0,
    turns             INT NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    working_s         INT NOT NULL DEFAULT 0,
    stopped_at_limits JSONB,
    open_question     TEXT,
    asked_at          TIMESTAMPTZ,
    answer            TEXT,
    summary           TEXT,
    follow_ups        JSONB,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_assignment_person
    ON assignments.assignment (person_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_assignment_status
    ON assignments.assignment (status, id);

CREATE TABLE IF NOT EXISTS assignments.checkpoint (
    id            BIGSERIAL PRIMARY KEY,
    assignment_id BIGINT NOT NULL REFERENCES assignments.assignment(id) ON DELETE CASCADE,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    note          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoint_assignment
    ON assignments.checkpoint (assignment_id, id DESC);

-- One row for each slice of model turns.
CREATE TABLE IF NOT EXISTS assignments.slice (
    id            BIGSERIAL PRIMARY KEY,
    assignment_id BIGINT NOT NULL REFERENCES assignments.assignment(id) ON DELETE CASCADE,
    n             INT NOT NULL,
    input         BIGINT NOT NULL DEFAULT 0,
    output        BIGINT NOT NULL DEFAULT 0,
    cache_read    BIGINT NOT NULL DEFAULT 0,
    cache_write   BIGINT NOT NULL DEFAULT 0,
    limits_before JSONB,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at      TIMESTAMPTZ,
    reason        TEXT,
    UNIQUE (assignment_id, n)
);
"""


class Store(Protocol):
    """What the manager needs from a store. Each row is a dict of its columns."""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def ping(self) -> bool: ...

    async def create(
        self,
        *,
        person_id: str,
        conversation_id: str | None,
        spec: dict[str, Any],
        model: str,
        page_path: Callable[[int], str],
    ) -> dict[str, Any]: ...

    async def get(self, assignment_id: int) -> dict[str, Any] | None: ...

    async def recent(
        self, person_id: str | None, limit: int, status: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def update(self, assignment_id: int, **fields: Any) -> dict[str, Any] | None: ...

    async def add_checkpoint(self, assignment_id: int, note: str) -> None: ...

    async def last_checkpoint(self, assignment_id: int) -> dict[str, Any] | None: ...

    async def counts(self) -> dict[str, int]: ...

    async def next_queued(self) -> dict[str, Any] | None: ...

    async def requeue_running(self) -> list[int]: ...


def check_fields(fields: dict[str, Any]) -> None:
    """Raise ValueError for a column ``update`` may not set, or a status that is not known."""
    unknown = set(fields) - UPDATABLE
    if unknown:
        raise ValueError(f"cannot update {', '.join(sorted(unknown))}")
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"unknown status {fields['status']!r}")


class PgStore:
    """The store in the shared Postgres, through an async connection pool."""

    def __init__(self, database_url: str, *, min_size: int = 1, max_size: int = 5) -> None:
        self._url = database_url
        self._min_size = min_size
        self._max_size = max_size
        self.pool: AsyncConnectionPool | None = None

    async def open(self) -> None:
        """Open the pool and apply the schema."""
        self.pool = AsyncConnectionPool(
            self._url,
            min_size=self._min_size,
            max_size=self._max_size,
            open=False,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
        await self.pool.open(wait=True, timeout=30)
        async with self.pool.connection() as conn:
            await conn.execute(SCHEMA_SQL)
        logger.info({"message": "assignments schema verified"})

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    def _pool(self) -> AsyncConnectionPool:
        if self.pool is None:
            raise RuntimeError("the store is not open")
        return self.pool

    async def ping(self) -> bool:
        try:
            async with self._pool().connection() as conn:
                await conn.execute("SELECT 1")
        except Exception:  # any failure means not ready
            return False
        return True

    async def create(
        self,
        *,
        person_id: str,
        conversation_id: str | None,
        spec: dict[str, Any],
        model: str,
        page_path: Callable[[int], str],
    ) -> dict[str, Any]:
        async with self._pool().connection() as conn, conn.transaction():
            cur = await conn.execute(
                "INSERT INTO assignments.assignment (person_id, conversation_id, spec, model) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (person_id, conversation_id, Jsonb(spec), model),
            )
            row = await cur.fetchone()
            assert row is not None
            cur = await conn.execute(
                "UPDATE assignments.assignment SET page_path = %s WHERE id = %s RETURNING *",
                (page_path(row["id"]), row["id"]),
            )
            created = await cur.fetchone()
        assert created is not None
        return dict(created)

    async def get(self, assignment_id: int) -> dict[str, Any] | None:
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM assignments.assignment WHERE id = %s", (assignment_id,)
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def recent(
        self, person_id: str | None, limit: int, status: str | None = None
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if person_id is not None:
            clauses.append("person_id = %s")
            params.append(person_id)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM assignments.assignment {where}ORDER BY id DESC LIMIT %s",
                (*params, limit),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    async def update(self, assignment_id: int, **fields: Any) -> dict[str, Any] | None:
        check_fields(fields)
        if not fields:
            return await self.get(assignment_id)
        names = sorted(fields)
        sets = ", ".join(f"{name} = %s" for name in names)
        values = [
            Jsonb(fields[n]) if n in JSON_FIELDS and fields[n] is not None else fields[n]
            for n in names
        ]
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                f"UPDATE assignments.assignment SET {sets} WHERE id = %s RETURNING *",
                (*values, assignment_id),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def add_checkpoint(self, assignment_id: int, note: str) -> None:
        async with self._pool().connection() as conn:
            await conn.execute(
                "INSERT INTO assignments.checkpoint (assignment_id, note) VALUES (%s, %s)",
                (assignment_id, note),
            )

    async def last_checkpoint(self, assignment_id: int) -> dict[str, Any] | None:
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "SELECT at, note FROM assignments.checkpoint WHERE assignment_id = %s "
                "ORDER BY id DESC LIMIT 1",
                (assignment_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def counts(self) -> dict[str, int]:
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "SELECT status, count(*) AS n FROM assignments.assignment GROUP BY status"
            )
            rows = await cur.fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    async def next_queued(self) -> dict[str, Any] | None:
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM assignments.assignment WHERE status = 'queued' ORDER BY id LIMIT 1"
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def requeue_running(self) -> list[int]:
        """Put each ``running`` assignment back in the queue. Returns their ids."""
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "UPDATE assignments.assignment SET status = 'queued' "
                "WHERE status = 'running' RETURNING id"
            )
            rows = await cur.fetchall()
        return sorted(int(row["id"]) for row in rows)
