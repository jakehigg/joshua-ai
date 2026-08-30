"""Data-access layer. All SQL lives here; callers take and return dataclasses.

The resolution primitives (``get_channel``, ``get_person_by_handle``,
``get_or_create_conversation``) turn a platform event into a
(channel, person, conversation) triple. This module owns the storage; the
channel adapters own the platform-specific policy.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import psycopg
from joshua_shared.log import get_logger
from psycopg.types.json import Json

from joshua_core.store.db import Database
from joshua_core.store.models import Channel, Conversation, Person, Task

logger = get_logger("store.repo")


def _channel(row: dict[str, Any]) -> Channel:
    return Channel(
        id=row["id"],
        channel_type=row["channel_type"],
        display_name=row.get("display_name"),
        default_person_id=row.get("default_person_id"),
        config=row.get("config") or {},
        session_mode=row.get("session_mode") or "per_person",
        created_at=row.get("created_at"),
    )


def _person(row: dict[str, Any]) -> Person:
    return Person(
        id=row["id"],
        display_name=row["display_name"],
        role=row.get("role") or "member",
        created_at=row.get("created_at"),
    )


def _conversation(row: dict[str, Any]) -> Conversation:
    return Conversation(
        id=str(row["id"]),
        channel_id=row["channel_id"],
        person_id=row.get("person_id"),
        sdk_session_id=row.get("sdk_session_id"),
        last_active_at=row.get("last_active_at"),
        last_channel_id=row.get("last_channel_id"),
        session_started_on=row.get("session_started_on"),
    )


def _task(row: dict[str, Any]) -> Task:
    return Task(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        prompt=row["prompt"],
        next_run_at=row["next_run_at"],
        cron_expr=row.get("cron_expr"),
        status=row.get("status") or "active",
        last_run_at=row.get("last_run_at"),
        last_error=row.get("last_error"),
        created_at=row.get("created_at"),
    )


class Repo:
    def __init__(self, db: Database):
        self._db = db

    @property
    def _pool(self):
        assert self._db.pool is not None, "Database not connected"
        return self._db.pool

    # --- channels ----------------------------------------------------------

    async def get_channel(self, channel_id: str) -> Channel | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM channels WHERE id = %s", (channel_id,))
            row = await cur.fetchone()
        return _channel(row) if row else None

    async def _find_channels(
        self,
        *,
        channel_type: str,
        session_mode: str,
        default_person_id: str | None = None,
    ) -> list[Channel]:
        sql = "SELECT * FROM channels WHERE channel_type = %s AND session_mode = %s"
        args: list[Any] = [channel_type, session_mode]
        if default_person_id is not None:
            sql += " AND default_person_id = %s"
            args.append(default_person_id)
        sql += " ORDER BY id"
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, tuple(args))
            rows = await cur.fetchall()
        return [_channel(r) for r in rows]

    async def resolve_channel(self, ref: str) -> Channel | None:
        """Resolve a channel reference to a live Channel row.

        The DB is the source of truth for channels (real ones auto-create on
        first inbound), so config and tools never hardcode platform chat ids.
        Accepts a literal id or a logical reference:

          - literal id            e.g. ``telegram:-1001234567890``
          - ``<type>:group``      the shared (group) channel of that type
          - ``<type>:dm:<person>``  that person's per-person channel of that type

        Returns ``None`` if nothing matches.
        """
        # 1) Literal id.
        ch = await self.get_channel(ref)
        if ch is not None:
            return ch

        # 2) Logical forms.
        parts = ref.split(":")
        if len(parts) == 2 and parts[1] == "group":
            matches = await self._find_channels(channel_type=parts[0], session_mode="shared")
            if len(matches) > 1:
                logger.warning(
                    {
                        "message": "ambiguous group ref; using first by id",
                        "ref": ref,
                        "candidates": [c.id for c in matches],
                    }
                )
            return matches[0] if matches else None
        if len(parts) == 3 and parts[1] == "dm":
            matches = await self._find_channels(
                channel_type=parts[0], session_mode="per_person", default_person_id=parts[2]
            )
            if len(matches) > 1:
                logger.warning(
                    {
                        "message": "ambiguous dm ref; using first by id",
                        "ref": ref,
                        "candidates": [c.id for c in matches],
                    }
                )
            return matches[0] if matches else None
        return None

    async def upsert_channel(
        self,
        channel_id: str,
        channel_type: str,
        display_name: str | None = None,
        default_person_id: str | None = None,
        config: dict[str, Any] | None = None,
        session_mode: str = "per_person",
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO channels (id, channel_type, display_name,
                                      default_person_id, config, session_mode)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    channel_type = EXCLUDED.channel_type,
                    display_name = EXCLUDED.display_name,
                    default_person_id = EXCLUDED.default_person_id,
                    config = EXCLUDED.config,
                    session_mode = EXCLUDED.session_mode
                """,
                (
                    channel_id,
                    channel_type,
                    display_name,
                    default_person_id,
                    Json(config or {}),
                    session_mode,
                ),
            )

    # --- people ------------------------------------------------------------

    async def get_person(self, person_id: str) -> Person | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM people WHERE id = %s", (person_id,))
            row = await cur.fetchone()
        return _person(row) if row else None

    async def list_people(self) -> list[Person]:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM people ORDER BY id")
            rows = await cur.fetchall()
        return [_person(r) for r in rows]

    async def upsert_person(self, person_id: str, display_name: str, role: str = "member") -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO people (id, display_name, role)
                VALUES (%s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    role = EXCLUDED.role
                """,
                (person_id, display_name, role),
            )

    async def get_person_by_handle(self, channel_type: str, handle: str) -> Person | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT p.* FROM people p
                JOIN person_handles h ON h.person_id = p.id
                WHERE h.channel_type = %s AND h.handle = %s
                """,
                (channel_type, handle),
            )
            row = await cur.fetchone()
        return _person(row) if row else None

    async def list_person_handles(self, channel_type: str) -> list[tuple[str, str]]:
        """All (handle, person_id) pairs for a channel type."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT handle, person_id FROM person_handles "
                "WHERE channel_type = %s ORDER BY handle",
                (channel_type,),
            )
            rows = await cur.fetchall()
        return [(r["handle"], r["person_id"]) for r in rows]

    async def upsert_person_handle(self, channel_type: str, handle: str, person_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO person_handles (channel_type, handle, person_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (channel_type, handle) DO UPDATE SET person_id = EXCLUDED.person_id
                """,
                (channel_type, handle, person_id),
            )

    async def handles_for_person(self, person_id: str) -> list[tuple[str, str]]:
        """All (channel_type, handle) pairs for a person, ordered."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT channel_type, handle FROM person_handles "
                "WHERE person_id = %s ORDER BY channel_type, handle",
                (person_id,),
            )
            rows = await cur.fetchall()
        return [(r["channel_type"], r["handle"]) for r in rows]

    async def remove_person(self, person_id: str) -> bool:
        """Mark a person ``removed`` and drop their handles. Keeps the row and its
        transcripts. Returns False when no such person exists."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE people SET role = 'removed' WHERE id = %s", (person_id,)
            )
            if cur.rowcount == 0:
                return False
            await conn.execute("DELETE FROM person_handles WHERE person_id = %s", (person_id,))
        return True

    # --- conversations -----------------------------------------------------

    async def _select_conversation(
        self, channel_id: str, person_id: str | None
    ) -> dict[str, Any] | None:
        async with self._pool.connection() as conn:
            if person_id is None:
                cur = await conn.execute(
                    "SELECT * FROM conversations WHERE channel_id = %s AND person_id IS NULL",
                    (channel_id,),
                )
            else:
                cur = await conn.execute(
                    "SELECT * FROM conversations WHERE channel_id = %s AND person_id = %s",
                    (channel_id, person_id),
                )
            return await cur.fetchone()

    async def get_or_create_conversation(
        self, channel_id: str, person_id: str | None
    ) -> Conversation:
        row = await self._select_conversation(channel_id, person_id)
        if row:
            return _conversation(row)
        try:
            async with self._pool.connection() as conn:
                cur = await conn.execute(
                    """
                    INSERT INTO conversations (channel_id, person_id)
                    VALUES (%s, %s)
                    RETURNING *
                    """,
                    (channel_id, person_id),
                )
                row = await cur.fetchone()
            return _conversation(row)
        except psycopg.errors.UniqueViolation:
            # Concurrent create lost the race — re-read the winner.
            row = await self._select_conversation(channel_id, person_id)
            assert row is not None
            return _conversation(row)

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM conversations WHERE id = %s", (conversation_id,)
            )
            row = await cur.fetchone()
        return _conversation(row) if row else None

    async def set_sdk_session(
        self, conversation_id: str, session_id: str, last_channel_id: str | None = None
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE conversations SET sdk_session_id = %s, last_active_at = now(), "
                "last_channel_id = COALESCE(%s, last_channel_id) WHERE id = %s",
                (session_id, last_channel_id, conversation_id),
            )

    async def touch_conversation(
        self, conversation_id: str, last_channel_id: str | None = None
    ) -> None:
        if last_channel_id is not None:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE conversations SET last_active_at = now(), "
                    "last_channel_id = %s WHERE id = %s",
                    (last_channel_id, conversation_id),
                )
            return
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE conversations SET last_active_at = now() WHERE id = %s",
                (conversation_id,),
            )

    async def rollover_sessions(self, today: date) -> int:
        """Drop the SDK session of every conversation not started on ``today``.

        Sets ``sdk_session_id = NULL`` and ``session_started_on = today`` for each
        conversation whose ``session_started_on`` is before ``today`` or never
        stamped. The next turn starts a fresh SDK session; continuity comes from
        the profile and recent posts in the system prompt. Returns the row count.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE conversations SET sdk_session_id = NULL, session_started_on = %s "
                "WHERE session_started_on IS NULL OR session_started_on < %s",
                (today, today),
            )
            return cur.rowcount

    # --- tasks -------------------------------------------------------------

    async def create_task(
        self,
        conversation_id: str,
        prompt: str,
        next_run_at: datetime,
        cron_expr: str | None = None,
    ) -> Task:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO tasks (conversation_id, prompt, cron_expr, next_run_at)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (conversation_id, prompt, cron_expr, next_run_at),
            )
            row = await cur.fetchone()
        return _task(row)

    async def get_task(self, task_id: str) -> Task | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
            row = await cur.fetchone()
        return _task(row) if row else None

    async def list_tasks(
        self, conversation_id: str | None = None, include_inactive: bool = False
    ) -> list[Task]:
        clauses = []
        params: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id = %s")
            params.append(conversation_id)
        if not include_inactive:
            clauses.append("status IN ('active', 'paused')")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT * FROM tasks{where} ORDER BY next_run_at", params)
            rows = await cur.fetchall()
        return [_task(r) for r in rows]

    async def claim_due_tasks(self, now: datetime, limit: int = 25) -> list[Task]:
        """Atomically grab due tasks so a slow turn can't double-fire.

        Marks claimed rows 'processing' under SKIP LOCKED; the scheduler resets
        them to active/completed after running.
        """
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE status = 'active' AND next_run_at <= %s
                    ORDER BY next_run_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (now, limit),
                )
                rows = await cur.fetchall()
                if rows:
                    ids = [r["id"] for r in rows]
                    await conn.execute(
                        "UPDATE tasks SET status = 'processing' WHERE id = ANY(%s)",
                        (ids,),
                    )
        return [_task(r) for r in rows]

    async def reschedule_task(
        self, task_id: str, next_run_at: datetime, error: str | None = None
    ) -> bool:
        """Re-arm a recurring task for its next run.

        Only rescheduled if the task is still 'processing' — if the agent moved
        it out of that state during the run (cancel -> 'completed', pause ->
        'paused'), that change is durable. Returns True if the row was
        rescheduled.
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE tasks
                SET status = 'active', next_run_at = %s, last_run_at = now(), last_error = %s
                WHERE id = %s AND status = 'processing'
                """,
                (next_run_at, error[:2000] if error else None, task_id),
            )
            return cur.rowcount > 0

    async def reset_processing(self) -> int:
        """Re-queue tasks stuck in 'processing' (e.g. process died mid-run)."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE tasks SET status = 'active' WHERE status = 'processing'"
            )
            return cur.rowcount

    async def complete_task(self, task_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE tasks SET status = 'completed', last_run_at = now() WHERE id = %s",
                (task_id,),
            )

    async def fail_task(self, task_id: str, error: str, reactivate: bool) -> None:
        status = "active" if reactivate else "failed"
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE tasks SET status = %s, last_run_at = now(), last_error = %s WHERE id = %s",
                (status, error[:2000], task_id),
            )

    async def set_task_status(
        self, task_id: str, status: str, conversation_id: str | None = None
    ) -> bool:
        """Set a task's status, optionally only if it belongs to a conversation.

        The agent-facing tools always pass ``conversation_id`` so a task can only
        be cancelled or paused from the conversation that owns it.
        """
        clauses = ["id = %s"]
        params: list[Any] = [status, task_id]
        if conversation_id:
            clauses.append("conversation_id = %s")
            params.append(conversation_id)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE tasks SET status = %s WHERE {' AND '.join(clauses)}", params
            )
            return cur.rowcount > 0

    async def update_task(
        self,
        task_id: str,
        prompt: str | None = None,
        cron_expr: str | None = None,
        next_run_at: datetime | None = None,
        conversation_id: str | None = None,
    ) -> bool:
        sets = []
        params: list[Any] = []
        if prompt is not None:
            sets.append("prompt = %s")
            params.append(prompt)
        if cron_expr is not None:
            sets.append("cron_expr = %s")
            params.append(cron_expr or None)
        if next_run_at is not None:
            sets.append("next_run_at = %s")
            params.append(next_run_at)
        if not sets:
            return False
        # Conversation-scoped for the same reason as set_task_status.
        clauses = ["id = %s"]
        params.append(task_id)
        if conversation_id:
            clauses.append("conversation_id = %s")
            params.append(conversation_id)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE {' AND '.join(clauses)}",
                params,
            )
            return cur.rowcount > 0

    # --- transcript --------------------------------------------------------

    async def add_transcript(
        self,
        conversation_id: str,
        direction: str,
        content: str,
        status: str = "ok",
        meta: dict[str, Any] | None = None,
    ) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO transcript (conversation_id, direction, content, status, meta)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (conversation_id, direction, content, status, Json(meta or {})),
            )

    async def recent_transcripts(self, since: datetime) -> list[dict[str, Any]]:
        """All transcript rows since ``since``, ordered for grouping by conversation."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT conversation_id, direction, content, status, meta, created_at
                FROM transcript
                WHERE created_at >= %s
                ORDER BY conversation_id, created_at
                """,
                (since,),
            )
            rows = await cur.fetchall()
        return [
            {
                "conversation_id": str(r["conversation_id"]),
                "direction": r["direction"],
                "content": r["content"],
                "status": r.get("status") or "ok",
                "meta": r.get("meta") or {},
                "created_at": r.get("created_at"),
            }
            for r in rows
        ]

    async def transcript_tail(self, conversation_id: str, limit: int = 40) -> list[dict[str, Any]]:
        """The last ``limit`` transcript rows for a conversation, oldest-first."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT * FROM (
                    SELECT direction, content, status, meta, created_at
                    FROM transcript
                    WHERE conversation_id = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                ) t ORDER BY created_at ASC
                """,
                (conversation_id, limit),
            )
            rows = await cur.fetchall()
        return [
            {
                "direction": r["direction"],
                "content": r["content"],
                "status": r.get("status") or "ok",
                "meta": r.get("meta") or {},
                "created_at": r.get("created_at"),
            }
            for r in rows
        ]

    async def read_conversation_meta(self, conversation_id: str) -> dict[str, Any] | None:
        """Conversation + channel labels for a conversation id."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT c.id, c.person_id, p.display_name, ch.id AS channel_id,
                       ch.channel_type, ch.display_name AS channel_name, ch.session_mode
                FROM conversations c
                LEFT JOIN channels ch ON ch.id = c.channel_id
                LEFT JOIN people p ON p.id = c.person_id
                WHERE c.id = %s
                """,
                (conversation_id,),
            )
            row = await cur.fetchone()
        return dict(row) if row else None

    async def latest_conversation_for(
        self, person_id: str, channel_like: str | None = None
    ) -> str | None:
        """The person's most-recently-active conversation id, optionally narrowed
        to a surface (channel id/type substring)."""
        clauses = ["c.person_id = %s"]
        params: list[Any] = [person_id]
        if channel_like is not None:
            clauses.append("(ch.id ILIKE %s OR ch.channel_type ILIKE %s)")
            params.extend([f"%{channel_like}%", f"%{channel_like}%"])
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""
                SELECT c.id FROM conversations c
                LEFT JOIN channels ch ON ch.id = c.channel_id
                WHERE {" AND ".join(clauses)}
                ORDER BY c.last_active_at DESC NULLS LAST
                LIMIT 1
                """,
                tuple(params),
            )
            row = await cur.fetchone()
        return str(row["id"]) if row else None

    # --- kb retrieval audit ------------------------------------------------

    async def add_kb_event(
        self,
        *,
        kind: str,
        query: str,
        conversation_id: str | None = None,
        turn_id: str | None = None,
        decision: str | None = None,
        prefer_recent: bool | None = None,
        best_sim: float | None = None,
        duration_ms: int | None = None,
        results: list[dict[str, Any]] | None = None,
    ) -> None:
        """One retrieval-audit row per injection decision or kb tool call.
        Callers wrap this in try/except — telemetry must never break a turn."""
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO kb_event
                    (conversation_id, turn_id, kind, query, decision,
                     prefer_recent, best_sim, duration_ms, results)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    conversation_id,
                    turn_id,
                    kind,
                    query,
                    decision,
                    prefer_recent,
                    best_sim,
                    duration_ms,
                    Json(results or []),
                ),
            )

    async def kb_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """The most recent retrieval-audit rows, newest first — backs
        ``GET /admin/kb/events`` and the shadow-mode comparison."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id, conversation_id, turn_id, kind, query, decision,
                       prefer_recent, best_sim, duration_ms, results, created_at
                FROM kb_event
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (limit,),
            )
            return await cur.fetchall()
