"""An in-memory store with the behaviour of ``PgStore``, for the offline tests."""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from typing import Any

from joshua_assignments.store import USAGE_FIELDS, check_fields, output_record

FIXED_NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


class FakeStore:
    def __init__(self, clock: Callable[[], datetime] = lambda: FIXED_NOW) -> None:
        self.clock = clock
        self.rows: dict[int, dict[str, Any]] = {}
        self.checkpoints: list[dict[str, Any]] = []
        self.slices: list[dict[str, Any]] = []
        self.saved: dict[int, dict[str, Any]] = {}
        self._next_saved = 1
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
        saved_id: int | None = None,
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
            "page_path": None,
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
            "plan": None,
            "plan_at": None,
            "report": None,
            "report_at": None,
            "outputs": None,
            "saved_id": saved_id,
        }
        self.rows[assignment_id] = row
        return copy.deepcopy(row)

    async def get(self, assignment_id: int) -> dict[str, Any] | None:
        row = self.rows.get(assignment_id)
        return copy.deepcopy(row) if row else None

    async def recent(
        self,
        person_id: str | None,
        limit: int,
        status: str | None = None,
        statuses: Collection[str] | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in sorted(self.rows.values(), key=lambda r: r["id"], reverse=True)
            if (person_id is None or row["person_id"] == person_id)
            and (status is None or row["status"] == status)
            and (statuses is None or row["status"] in statuses)
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

    async def write_plan(self, assignment_id: int, markdown: str) -> None:
        row = self.rows.get(assignment_id)
        if row is not None:
            row.update(plan=markdown, plan_at=self.clock())

    async def read_plan(self, assignment_id: int) -> str | None:
        row = self.rows.get(assignment_id)
        return row["plan"] if row else None

    async def add_output(self, assignment_id: int, kind: str, ref: str, title: str) -> int:
        row = self.rows.get(assignment_id)
        if row is None:
            return 0
        kind, ref, title = output_record(kind, ref, title)
        outputs = row["outputs"] = list(row["outputs"] or [])
        outputs.append({"kind": kind, "ref": ref, "title": title, "at": self.clock().isoformat()})
        return len(outputs)

    async def write_report(self, assignment_id: int, markdown: str) -> None:
        row = self.rows.get(assignment_id)
        if row is not None:
            row.update(report=markdown, report_at=self.clock())

    def _with_runs(self, saved: dict[str, Any]) -> dict[str, Any]:
        runs = [
            r
            for r in self.rows.values()
            if r["saved_id"] == saved["id"] and r["person_id"] == saved["person_id"]
        ]
        last = max(runs, key=lambda r: r["id"], default=None)
        return copy.deepcopy(
            {
                **saved,
                "runs": len(runs),
                "last_run": (
                    {"id": last["id"], "status": last["status"], "finished_at": last["finished_at"]}
                    if last
                    else None
                ),
            }
        )

    async def put_saved(self, person_id: str, name: str, spec: dict[str, Any]) -> dict[str, Any]:
        for saved in self.saved.values():
            if saved["person_id"] == person_id and saved["name"] == name:
                saved.update(spec=copy.deepcopy(spec), updated_at=self.clock())
                return {**copy.deepcopy(saved), "created": False}
        saved = {
            "id": self._next_saved,
            "person_id": person_id,
            "name": name,
            "spec": copy.deepcopy(spec),
            "created_at": self.clock(),
            "updated_at": self.clock(),
        }
        self._next_saved += 1
        self.saved[saved["id"]] = saved
        return {**copy.deepcopy(saved), "created": True}

    async def find_saved(self, person_id: str, name: str) -> dict[str, Any] | None:
        for saved in self.saved.values():
            if saved["person_id"] == person_id and saved["name"] == name:
                return self._with_runs(saved)
        return None

    async def get_saved(self, saved_id: int) -> dict[str, Any] | None:
        saved = self.saved.get(saved_id)
        return self._with_runs(saved) if saved else None

    async def list_saved(self, person_id: str) -> list[dict[str, Any]]:
        mine = [s for s in self.saved.values() if s["person_id"] == person_id]
        return [self._with_runs(s) for s in sorted(mine, key=lambda s: s["id"], reverse=True)]

    async def delete_saved(self, person_id: str, name: str) -> bool:
        for saved_id, saved in list(self.saved.items()):
            if saved["person_id"] == person_id and saved["name"] == name:
                del self.saved[saved_id]
                for row in self.rows.values():
                    if row["saved_id"] == saved_id:
                        row["saved_id"] = None
                return True
        return False

    async def saved_names(self, saved_ids: Collection[int]) -> dict[int, str]:
        return {i: self.saved[i]["name"] for i in saved_ids if i in self.saved}

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

    async def next_slice_n(self, assignment_id: int) -> int:
        mine = [s["n"] for s in self.slices if s["assignment_id"] == assignment_id]
        return max(mine, default=0) + 1

    async def add_slice(self, assignment_id: int, **fields: Any) -> None:
        if any(s["assignment_id"] == assignment_id and s["n"] == fields["n"] for s in self.slices):
            return
        self.slices.append({"assignment_id": assignment_id, **copy.deepcopy(fields)})
