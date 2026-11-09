"""The store of the assignments: schema ``assignments`` in the shared Postgres.

One SQL constant, applied at start with ``CREATE SCHEMA IF NOT EXISTS``,
``CREATE TABLE IF NOT EXISTS``, and ``ADD COLUMN IF NOT EXISTS``. It is
additive only: a release never removes a table or a column. The schema keeps
the tables of this container apart from the tables of core in the same
database.

``Store`` is the protocol the manager uses. ``PgStore`` is the Postgres
store. It takes a connection from a pool for each operation, as core does,
so two operations never share a connection. A cancelled caller does not stop
an operation: the operation runs to its end in a task of its own and gives
its connection back clean. Every method is safe to call concurrently.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Collection, Coroutine
from datetime import datetime
from typing import Any, Concatenate, Protocol

from joshua_shared.log import get_logger
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

logger = get_logger("joshua_assignments.store")

# How long ``close`` waits for the operations that are still running.
CLOSE_WAIT_S = 10.0

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
# An assignment that ended. Only ``extend_assignment`` starts one of
# ``RESUMABLE`` again; nothing starts the others.
FINISHED = frozenset({"done", "failed", "cancelled", "budget_exceeded", "time_exceeded"})
# An ended assignment that ``extend_assignment`` resumes when it raises a cap.
RESUMABLE = frozenset({"budget_exceeded", "time_exceeded"})
# An ended assignment whose scratch stays for ``assignments.keep_failed_work``.
KEEP_SCRATCH = frozenset({"failed", *RESUMABLE})
# What a person sees: three states. ``state`` maps each status to one.
STATES = ("queued", "in_progress", "completed")
IN_PROGRESS = frozenset({"running", *PARKED})
USAGE_FIELDS = ("input", "output", "cache_read", "cache_write")
JSON_FIELDS = frozenset({"spec", "stopped_at_limits", "follow_ups", "outputs"})
# The caps of one output. ``add_output`` cuts each field to its cap.
MAX_OUTPUT_KIND = 40
MAX_OUTPUT_REF = 500
MAX_OUTPUT_TITLE = 200
# The columns ``update`` may set.
UPDATABLE = frozenset(
    {
        "spec",
        "status",
        "session_id",
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
    -- No longer written. A row from an earlier release keeps its value.
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
-- The plan of the worker (Markdown, replaced whole), the report (Markdown),
-- and the outputs: a list of {"kind", "ref", "title", "at"}.
ALTER TABLE assignments.assignment ADD COLUMN IF NOT EXISTS plan TEXT;
ALTER TABLE assignments.assignment ADD COLUMN IF NOT EXISTS plan_at TIMESTAMPTZ;
ALTER TABLE assignments.assignment ADD COLUMN IF NOT EXISTS report TEXT;
ALTER TABLE assignments.assignment ADD COLUMN IF NOT EXISTS report_at TIMESTAMPTZ;
ALTER TABLE assignments.assignment ADD COLUMN IF NOT EXISTS outputs JSONB;
CREATE INDEX IF NOT EXISTS idx_assignment_person
    ON assignments.assignment (person_id, id DESC);

-- A saved assignment: a spec with a name and no run, started by name.
CREATE TABLE IF NOT EXISTS assignments.saved (
    id          BIGSERIAL PRIMARY KEY,
    person_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    spec        JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (person_id, name)
);
-- The saved assignment that a run started from, or NULL.
ALTER TABLE assignments.assignment ADD COLUMN IF NOT EXISTS saved_id BIGINT
    REFERENCES assignments.saved(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_assignment_saved
    ON assignments.assignment (saved_id, id DESC);
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


# A saved assignment with the count of its runs and its newest run. A run
# counts only when it belongs to the person of the saved assignment.
SAVED_SELECT = """
SELECT s.*, COALESCE(r.runs, 0) AS runs,
       l.id AS last_run_id, l.status AS last_run_status, l.finished_at AS last_run_finished_at
FROM assignments.saved s
LEFT JOIN LATERAL (
    SELECT count(*) AS runs FROM assignments.assignment a
    WHERE a.saved_id = s.id AND a.person_id = s.person_id
) r ON true
LEFT JOIN LATERAL (
    SELECT a.id, a.status, a.finished_at FROM assignments.assignment a
    WHERE a.saved_id = s.id AND a.person_id = s.person_id
    ORDER BY a.id DESC LIMIT 1
) l ON true
WHERE {where}
ORDER BY s.id DESC
"""


def saved_row(row: dict[str, Any]) -> dict[str, Any]:
    """A row of ``SAVED_SELECT`` with ``runs`` an int and ``last_run`` a dict or None."""
    last_id = row.pop("last_run_id", None)
    last_status = row.pop("last_run_status", None)
    last_finished = row.pop("last_run_finished_at", None)
    row["runs"] = int(row.get("runs") or 0)
    row["last_run"] = (
        {"id": int(last_id), "status": last_status, "finished_at": last_finished}
        if last_id is not None
        else None
    )
    return row


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
        saved_id: int | None = None,
    ) -> dict[str, Any]: ...

    async def get(self, assignment_id: int) -> dict[str, Any] | None: ...

    async def recent(
        self,
        person_id: str | None,
        limit: int,
        status: str | None = None,
        statuses: Collection[str] | None = None,
        saved_id: int | None = None,
    ) -> list[dict[str, Any]]: ...

    async def update(self, assignment_id: int, **fields: Any) -> dict[str, Any] | None: ...

    async def add_checkpoint(self, assignment_id: int, note: str) -> None: ...

    async def last_checkpoint(self, assignment_id: int) -> dict[str, Any] | None: ...

    async def checkpoints(self, assignment_id: int, limit: int) -> list[dict[str, Any]]: ...

    async def write_plan(self, assignment_id: int, markdown: str) -> None: ...

    async def read_plan(self, assignment_id: int) -> str | None: ...

    async def add_output(self, assignment_id: int, kind: str, ref: str, title: str) -> int: ...

    async def write_report(self, assignment_id: int, markdown: str) -> None: ...

    async def put_saved(
        self, person_id: str, name: str, spec: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def find_saved(self, person_id: str, name: str) -> dict[str, Any] | None: ...

    async def get_saved(self, saved_id: int) -> dict[str, Any] | None: ...

    async def list_saved(self, person_id: str) -> list[dict[str, Any]]: ...

    async def delete_saved(self, person_id: str, name: str) -> bool: ...

    async def saved_names(self, saved_ids: Collection[int]) -> dict[int, str]: ...

    async def counts(self) -> dict[str, int]: ...

    async def next_queued(self) -> dict[str, Any] | None: ...

    async def requeue_running(self) -> list[int]: ...

    async def next_slice_n(self, assignment_id: int) -> int: ...

    async def add_slice(
        self,
        assignment_id: int,
        *,
        n: int,
        usage: dict[str, int],
        limits_before: dict[str, Any] | None,
        started_at: datetime,
        ended_at: datetime,
        reason: str,
    ) -> None: ...


def state(status: str) -> str:
    """The state that a person sees for ``status``: queued, in_progress, or completed."""
    if status == "queued":
        return "queued"
    if status in IN_PROGRESS:
        return "in_progress"
    return "completed"


def statuses_of(state_name: str) -> tuple[str, ...]:
    """The statuses of one state, in the order of ``STATUSES``."""
    return tuple(status for status in STATUSES if state(status) == state_name)


def output_record(kind: str, ref: str, title: str) -> tuple[str, str, str]:
    """``kind``, ``ref``, and ``title``, each stripped and cut to its cap."""
    return (
        kind.strip()[:MAX_OUTPUT_KIND],
        ref.strip()[:MAX_OUTPUT_REF],
        title.strip()[:MAX_OUTPUT_TITLE],
    )


def check_fields(fields: dict[str, Any]) -> None:
    """Raise ValueError for a column ``update`` may not set, or a status that is not known."""
    unknown = set(fields) - UPDATABLE
    if unknown:
        raise ValueError(f"cannot update {', '.join(sorted(unknown))}")
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"unknown status {fields['status']!r}")


def _settle(task: asyncio.Task[Any]) -> None:
    """Log the error of an operation whose caller was cancelled, so it is not lost."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning({"message": "a store operation failed", "error": type(exc).__name__})


def shielded[**P, R](
    method: Callable[Concatenate[PgStore, P], Coroutine[Any, Any, R]],
) -> Callable[Concatenate[PgStore, P], Awaitable[R]]:
    """Run a store method in its own task, so a cancelled caller does not stop it.

    psycopg cancels a query when its task is cancelled. When the task is
    cancelled again during that cleanup (an anyio cancel scope cancels at
    each await, and the SDK can cancel a tool call two times), the connection
    stays busy. The rollback then fails with "another command is already in
    progress", and the pool discards the connection. The caller still gets
    the cancellation at once.
    """

    @functools.wraps(method)
    async def run(self: PgStore, *args: P.args, **kwargs: P.kwargs) -> R:
        task = asyncio.ensure_future(method(self, *args, **kwargs))
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_settle)
            raise

    return run


class PgStore:
    """The store in the shared Postgres, through an async connection pool."""

    def __init__(self, database_url: str, *, min_size: int = 1, max_size: int = 5) -> None:
        self._url = database_url
        self._min_size = min_size
        self._max_size = max_size
        self.pool: AsyncConnectionPool | None = None
        self._running: set[asyncio.Task[Any]] = set()

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
        """Wait for the running operations, then close the pool."""
        if self._running:
            await asyncio.wait(set(self._running), timeout=CLOSE_WAIT_S)
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    def _pool(self) -> AsyncConnectionPool:
        if self.pool is None:
            raise RuntimeError("the store is not open")
        return self.pool

    @shielded
    async def ping(self) -> bool:
        try:
            async with self._pool().connection() as conn:
                await conn.execute("SELECT 1")
        except Exception:  # any failure means not ready
            return False
        return True

    @shielded
    async def create(
        self,
        *,
        person_id: str,
        conversation_id: str | None,
        spec: dict[str, Any],
        model: str,
        saved_id: int | None = None,
    ) -> dict[str, Any]:
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "INSERT INTO assignments.assignment "
                "(person_id, conversation_id, spec, model, saved_id) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (person_id, conversation_id, Jsonb(spec), model, saved_id),
            )
            created = await cur.fetchone()
        assert created is not None
        return dict(created)

    @shielded
    async def get(self, assignment_id: int) -> dict[str, Any] | None:
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM assignments.assignment WHERE id = %s", (assignment_id,)
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    @shielded
    async def recent(
        self,
        person_id: str | None,
        limit: int,
        status: str | None = None,
        statuses: Collection[str] | None = None,
        saved_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """The newest rows. ``status``, ``statuses``, and ``saved_id`` filter, and all apply."""
        clauses: list[str] = []
        params: list[Any] = []
        if person_id is not None:
            clauses.append("person_id = %s")
            params.append(person_id)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if statuses is not None:
            clauses.append("status = ANY(%s)")
            params.append(list(statuses))
        if saved_id is not None:
            clauses.append("saved_id = %s")
            params.append(saved_id)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM assignments.assignment {where}ORDER BY id DESC LIMIT %s",
                (*params, limit),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    @shielded
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

    @shielded
    async def add_checkpoint(self, assignment_id: int, note: str) -> None:
        async with self._pool().connection() as conn:
            await conn.execute(
                "INSERT INTO assignments.checkpoint (assignment_id, note) VALUES (%s, %s)",
                (assignment_id, note),
            )

    @shielded
    async def last_checkpoint(self, assignment_id: int) -> dict[str, Any] | None:
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "SELECT at, note FROM assignments.checkpoint WHERE assignment_id = %s "
                "ORDER BY id DESC LIMIT 1",
                (assignment_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    @shielded
    async def checkpoints(self, assignment_id: int, limit: int) -> list[dict[str, Any]]:
        """The newest ``limit`` checkpoints of an assignment, the newest first."""
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "SELECT at, note FROM assignments.checkpoint WHERE assignment_id = %s "
                "ORDER BY id DESC LIMIT %s",
                (assignment_id, limit),
            )
            rows = await cur.fetchall()
        return [dict(row) for row in rows]

    @shielded
    async def write_plan(self, assignment_id: int, markdown: str) -> None:
        """Replace the plan of the assignment."""
        async with self._pool().connection() as conn:
            await conn.execute(
                "UPDATE assignments.assignment SET plan = %s, plan_at = now() WHERE id = %s",
                (markdown, assignment_id),
            )

    @shielded
    async def read_plan(self, assignment_id: int) -> str | None:
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "SELECT plan FROM assignments.assignment WHERE id = %s", (assignment_id,)
            )
            row = await cur.fetchone()
        return row["plan"] if row else None

    @shielded
    async def add_output(self, assignment_id: int, kind: str, ref: str, title: str) -> int:
        """Append one output to the list. Returns the number of outputs, 0 for no row."""
        kind, ref, title = output_record(kind, ref, title)
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "UPDATE assignments.assignment SET outputs = COALESCE(outputs, '[]'::jsonb) || "
                "jsonb_build_array(jsonb_build_object("
                "'kind', %s::text, 'ref', %s::text, 'title', %s::text, 'at', now())) "
                "WHERE id = %s RETURNING jsonb_array_length(outputs) AS n",
                (kind, ref, title, assignment_id),
            )
            row = await cur.fetchone()
        return int(row["n"]) if row else 0

    @shielded
    async def write_report(self, assignment_id: int, markdown: str) -> None:
        """Replace the report of the assignment."""
        async with self._pool().connection() as conn:
            await conn.execute(
                "UPDATE assignments.assignment SET report = %s, report_at = now() WHERE id = %s",
                (markdown, assignment_id),
            )

    @shielded
    async def put_saved(self, person_id: str, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        """Create the saved assignment ``name`` of the person, or replace its spec.

        The row has ``created``: True for a new saved assignment, False for a
        replaced one.
        """
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "INSERT INTO assignments.saved (person_id, name, spec) VALUES (%s, %s, %s) "
                "ON CONFLICT (person_id, name) DO UPDATE "
                "SET spec = EXCLUDED.spec, updated_at = now() "
                "RETURNING *, (xmax = 0) AS created",
                (person_id, name, Jsonb(spec)),
            )
            row = await cur.fetchone()
        assert row is not None
        return dict(row)

    @shielded
    async def find_saved(self, person_id: str, name: str) -> dict[str, Any] | None:
        """The saved assignment ``name`` of the person, with its runs, or None."""
        return await self._one_saved("s.person_id = %s AND s.name = %s", (person_id, name))

    @shielded
    async def get_saved(self, saved_id: int) -> dict[str, Any] | None:
        """One saved assignment, with its runs, or None."""
        return await self._one_saved("s.id = %s", (saved_id,))

    async def _one_saved(self, where: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        rows = await self._saved_rows(where, params)
        return rows[0] if rows else None

    async def _saved_rows(self, where: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        async with self._pool().connection() as conn:
            cur = await conn.execute(SAVED_SELECT.format(where=where), params)
            rows = await cur.fetchall()
        return [saved_row(dict(row)) for row in rows]

    @shielded
    async def list_saved(self, person_id: str) -> list[dict[str, Any]]:
        """The saved assignments of the person, the newest first, each with its runs."""
        return await self._saved_rows("s.person_id = %s", (person_id,))

    @shielded
    async def delete_saved(self, person_id: str, name: str) -> bool:
        """Delete one saved assignment. Its runs stay, with no saved assignment."""
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "DELETE FROM assignments.saved WHERE person_id = %s AND name = %s RETURNING id",
                (person_id, name),
            )
            row = await cur.fetchone()
        return row is not None

    @shielded
    async def saved_names(self, saved_ids: Collection[int]) -> dict[int, str]:
        """The name of each saved assignment in ``saved_ids`` that exists."""
        if not saved_ids:
            return {}
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "SELECT id, name FROM assignments.saved WHERE id = ANY(%s)",
                (list(saved_ids),),
            )
            rows = await cur.fetchall()
        return {int(row["id"]): row["name"] for row in rows}

    @shielded
    async def counts(self) -> dict[str, int]:
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "SELECT status, count(*) AS n FROM assignments.assignment GROUP BY status"
            )
            rows = await cur.fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    @shielded
    async def next_queued(self) -> dict[str, Any] | None:
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM assignments.assignment WHERE status = 'queued' ORDER BY id LIMIT 1"
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    @shielded
    async def requeue_running(self) -> list[int]:
        """Put each ``running`` assignment back in the queue. Returns their ids."""
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "UPDATE assignments.assignment SET status = 'queued' "
                "WHERE status = 'running' RETURNING id"
            )
            rows = await cur.fetchall()
        return sorted(int(row["id"]) for row in rows)

    @shielded
    async def next_slice_n(self, assignment_id: int) -> int:
        """The number of the next slice of an assignment: 1 for the first."""
        async with self._pool().connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(MAX(n), 0) + 1 AS n FROM assignments.slice "
                "WHERE assignment_id = %s",
                (assignment_id,),
            )
            row = await cur.fetchone()
        return int(row["n"]) if row else 1

    @shielded
    async def add_slice(
        self,
        assignment_id: int,
        *,
        n: int,
        usage: dict[str, int],
        limits_before: dict[str, Any] | None,
        started_at: datetime,
        ended_at: datetime,
        reason: str,
    ) -> None:
        """Store one slice. A slice with the same number is not stored again."""
        async with self._pool().connection() as conn:
            await conn.execute(
                "INSERT INTO assignments.slice (assignment_id, n, input, output, cache_read, "
                "cache_write, limits_before, started_at, ended_at, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (assignment_id, n) DO NOTHING",
                (
                    assignment_id,
                    n,
                    *(int(usage.get(name, 0)) for name in USAGE_FIELDS),
                    Jsonb(limits_before) if limits_before is not None else None,
                    started_at,
                    ended_at,
                    reason,
                ),
            )
