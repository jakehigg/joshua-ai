"""An in-memory store with the behaviour of ``PgStore``, for the offline tests."""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from joshua_assignments.store import USAGE_FIELDS, check_fields

FIXED_NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


class FakeStore:
    def __init__(self, clock: Callable[[], datetime] = lambda: FIXED_NOW) -> None:
        self.clock = clock
        self.rows: dict[int, dict[str, Any]] = {}
        self.checkpoints: list[dict[str, Any]] = []
        self.opened = False
        self.closed = False
        self.healthy = True
        self._next = 1

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def ping(self) -> bool:
        return self.healthy

    async def create(
        self,
        *,
        person_id: str,
        conversation_id: str | None,
        spec: dict[str, Any],
        model: str,
        page_path: Callable[[int], str],
    ) -> dict[str, Any]:
        assignment_id = self._next
        self._next += 1
        row: dict[str, Any] = {
            "id": assignment_id,
            "person_id": person_id,
            "conversation_id": conversation_id,
            "spec": copy.deepcopy(spec),
            "status": "queued",
            "model": model,
            "session_id": None,
            "page_path": page_path(assignment_id),
            **{name: 0 for name in USAGE_FIELDS},
            "turns": 0,
            "created_at": self.clock(),
            "started_at": None,
            "finished_at": None,
            "working_s": 0,
            "stopped_at_limits": None,
            "open_question": None,
            "asked_at": None,
            "answer": None,
            "summary": None,
            "follow_ups": None,
            "error": None,
        }
        self.rows[assignment_id] = row
        return copy.deepcopy(row)

    async def get(self, assignment_id: int) -> dict[str, Any] | None:
        row = self.rows.get(assignment_id)
        return copy.deepcopy(row) if row else None

    async def recent(
        self, person_id: str | None, limit: int, status: str | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in sorted(self.rows.values(), key=lambda r: r["id"], reverse=True)
            if (person_id is None or row["person_id"] == person_id)
            and (status is None or row["status"] == status)
        ]
        return copy.deepcopy(rows[:limit])

    async def update(self, assignment_id: int, **fields: Any) -> dict[str, Any] | None:
        check_fields(fields)
        row = self.rows.get(assignment_id)
        if row is None:
            return None
        row.update(copy.deepcopy(fields))
        return copy.deepcopy(row)

    async def add_checkpoint(self, assignment_id: int, note: str) -> None:
        self.checkpoints.append({"assignment_id": assignment_id, "at": self.clock(), "note": note})

    async def last_checkpoint(self, assignment_id: int) -> dict[str, Any] | None:
        mine = [c for c in self.checkpoints if c["assignment_id"] == assignment_id]
        return {"at": mine[-1]["at"], "note": mine[-1]["note"]} if mine else None

    async def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.rows.values():
            out[row["status"]] = out.get(row["status"], 0) + 1
        return out

    async def next_queued(self) -> dict[str, Any] | None:
        queued = [r for r in self.rows.values() if r["status"] == "queued"]
        return copy.deepcopy(min(queued, key=lambda r: r["id"])) if queued else None

    async def requeue_running(self) -> list[int]:
        ids = []
        for row in self.rows.values():
            if row["status"] == "running":
                row["status"] = "queued"
                ids.append(row["id"])
        return sorted(ids)
