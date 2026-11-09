"""An in-memory store with the behaviour of ``PgStore``, for the offline tests."""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from typing import Any

from joshua_assignments.store import USAGE_FIELDS, Parent, check_fields, output_record

FIXED_NOW = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


class FakeStore:
    def __init__(self, clock: Callable[[], datetime] = lambda: FIXED_NOW) -> None:
        self.clock = clock
        self.rows: dict[int, dict[str, Any]] = {}
        self.checkpoint_rows: list[dict[str, Any]] = []
        self.slices: list[dict[str, Any]] = []
        self.saved: dict[int, dict[str, Any]] = {}
        self._next_saved = 1
        self.comment_rows: dict[int, dict[str, Any]] = {}
        self._next_comment = 1
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
        status: str = "draft",
        saved_id: int | None = None,
        parent_id: int | None = None,
    ) -> dict[str, Any]:
        check_fields({"status": status})
        assignment_id = self._next
        self._next += 1
        row: dict[str, Any] = {
            "id": assignment_id,
            "person_id": person_id,
            "conversation_id": conversation_id,
            "spec": copy.deepcopy(spec),
            "status": status,
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
            "open_request": None,
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
            "parent_id": parent_id,
            "granted_repos": None,
            "inbox": None,
            "deleted_at": None,
        }
        self.rows[assignment_id] = row
        return copy.deepcopy(row)

    async def get(self, assignment_id: int) -> dict[str, Any] | None:
        row = self.rows.get(assignment_id)
        return copy.deepcopy(row) if row else None

    async def lineage(self, assignment_id: int) -> list[dict[str, Any]]:
        row = self.rows.get(assignment_id)
        if row is None:
            return []
        while row["parent_id"] is not None and row["parent_id"] in self.rows:
            row = self.rows[row["parent_id"]]
        ids = {row["id"]}
        for other in sorted(self.rows.values(), key=lambda r: r["id"]):
            if other["parent_id"] in ids:
                ids.add(other["id"])
        keys = ("id", "person_id", "parent_id", "spec", "status", "created_at", "finished_at")
        return [
            {key: copy.deepcopy(self.rows[i][key]) for key in (*keys, "deleted_at")}
            for i in sorted(ids)
        ][:50]

    async def recent(
        self,
        person_id: str | None,
        limit: int,
        status: str | None = None,
        statuses: Collection[str] | None = None,
        saved_id: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in sorted(self.rows.values(), key=lambda r: r["id"], reverse=True)
            if row["deleted_at"] is None
            and (person_id is None or row["person_id"] == person_id)
            and (status is None or row["status"] == status)
            and (statuses is None or row["status"] in statuses)
            and (saved_id is None or row["saved_id"] == saved_id)
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
        self.checkpoint_rows.append(
            {"assignment_id": assignment_id, "at": self.clock(), "note": note}
        )

    async def last_checkpoint(self, assignment_id: int) -> dict[str, Any] | None:
        mine = [c for c in self.checkpoint_rows if c["assignment_id"] == assignment_id]
        return {"at": mine[-1]["at"], "note": mine[-1]["note"]} if mine else None

    async def checkpoints(self, assignment_id: int, limit: int) -> list[dict[str, Any]]:
        mine = [c for c in self.checkpoint_rows if c["assignment_id"] == assignment_id]
        return [{"at": c["at"], "note": c["note"]} for c in reversed(mine)][:limit]

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
            if r["saved_id"] == saved["id"]
            and r["person_id"] == saved["person_id"]
            and r["status"] != "draft"
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
                for cid in [c for c, r in self.comment_rows.items() if r["saved_id"] == saved_id]:
                    del self.comment_rows[cid]
                return True
        return False

    async def saved_names(self, saved_ids: Collection[int]) -> dict[int, str]:
        return {i: self.saved[i]["name"] for i in saved_ids if i in self.saved}

    async def add_comment(
        self,
        person_id: str,
        parent: Parent,
        *,
        target: str,
        line_no: int,
        line_text: str,
        body: str,
    ) -> dict[str, Any]:
        row = {
            "id": self._next_comment,
            "person_id": person_id,
            "saved_id": parent.id if parent.kind == "saved" else None,
            "assignment_id": parent.id if parent.kind == "assignment" else None,
            "target": target,
            "line_no": line_no,
            "line_text": line_text,
            "body": body,
            "created_at": self.clock(),
            "resolved_at": None,
            "resolution": None,
            "applied": None,
            "sent_at": None,
        }
        self._next_comment += 1
        self.comment_rows[row["id"]] = row
        return copy.deepcopy(row)

    async def comments(
        self, person_id: str, parent: Parent, *, open_only: bool = False
    ) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(row)
            for _, row in sorted(self.comment_rows.items())
            if row["person_id"] == person_id
            and row[parent.column] == parent.id
            and not (open_only and row["resolved_at"] is not None)
        ]

    async def get_comment(self, person_id: str, comment_id: int) -> dict[str, Any] | None:
        row = self.comment_rows.get(comment_id)
        return copy.deepcopy(row) if row and row["person_id"] == person_id else None

    async def delete_comment(self, person_id: str, parent: Parent, comment_id: int) -> bool:
        row = self.comment_rows.get(comment_id)
        if (
            row is None
            or row["person_id"] != person_id
            or row[parent.column] != parent.id
            or row["resolved_at"] is not None
        ):
            return False
        del self.comment_rows[comment_id]
        return True

    async def resolve_comment(
        self,
        person_id: str,
        comment_id: int,
        resolution: str,
        applied: list[str] | None = None,
    ) -> dict[str, Any] | None:
        row = self.comment_rows.get(comment_id)
        if row is None or row["person_id"] != person_id or row["resolved_at"] is not None:
            return None
        row.update(resolved_at=self.clock(), resolution=resolution, applied=copy.deepcopy(applied))
        return copy.deepcopy(row)

    async def open_comment_counts(
        self, person_id: str, saved_ids: Collection[int]
    ) -> dict[int, int]:
        out: dict[int, int] = {}
        for row in self.comment_rows.values():
            if (
                row["person_id"] == person_id
                and row["saved_id"] in saved_ids
                and row["resolved_at"] is None
            ):
                out[row["saved_id"]] = out.get(row["saved_id"], 0) + 1
        return out

    async def unsent_comments(self, assignment_id: int | None = None) -> list[dict[str, Any]]:
        return [
            copy.deepcopy(row)
            for _, row in sorted(self.comment_rows.items())
            if row["assignment_id"] is not None
            and row["target"] in ("plan", "report")
            and row["sent_at"] is None
            and row["resolved_at"] is None
            and (assignment_id is None or row["assignment_id"] == assignment_id)
        ]

    async def mark_comments_sent(self, comment_ids: Collection[int]) -> None:
        for comment_id in comment_ids:
            if comment_id in self.comment_rows:
                self.comment_rows[comment_id]["sent_at"] = self.clock()

    async def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.rows.values():
            if row["deleted_at"] is not None:
                continue
            out[row["status"]] = out.get(row["status"], 0) + 1
        return out

    async def running(self) -> list[dict[str, Any]]:
        rows = [r for r in self.rows.values() if r["status"] == "running"]
        return copy.deepcopy(sorted(rows, key=lambda r: r["id"]))

    async def delete_draft(self, assignment_id: int) -> bool:
        row = self.rows.get(assignment_id)
        if (
            row is None
            or row["status"] != "draft"
            or row["started_at"] is not None
            or row["deleted_at"] is not None
        ):
            return False
        row["deleted_at"] = self.clock()
        return True

    async def restore_draft(self, assignment_id: int) -> dict[str, Any] | None:
        row = self.rows.get(assignment_id)
        if row is None or row["status"] != "draft" or row["deleted_at"] is None:
            return None
        row["deleted_at"] = None
        return copy.deepcopy(row)

    async def next_slice_n(self, assignment_id: int) -> int:
        mine = [s["n"] for s in self.slices if s["assignment_id"] == assignment_id]
        return max(mine, default=0) + 1

    async def add_slice(self, assignment_id: int, **fields: Any) -> None:
        if any(s["assignment_id"] == assignment_id and s["n"] == fields["n"] for s in self.slices):
            return
        self.slices.append({"assignment_id": assignment_id, **copy.deepcopy(fields)})
