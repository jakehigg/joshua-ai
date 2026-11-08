"""The manager: the rules behind the six tools.

The manager validates a spec, stores the assignment, and starts it when a
slot is free. ``assignments.max_concurrent`` counts ``running`` assignments
only, so a parked assignment holds no slot. When the runner stops an
assignment, ``stopped`` sends the report or the question to channels and
starts the next queued assignment.

A parked assignment (``waiting`` or ``paused_limits``) waits with no time
limit. Only ``answer_assignment`` starts it again, and only when the limits
are under the ceilings. ``extend_assignment`` starts a ``budget_exceeded`` or
``time_exceeded`` assignment again. At start, each ``running`` assignment
goes back to the queue and resumes its session with a note that the
container restarted.

A rejected call returns ``{"status": "rejected", "reason", "message"}``. The
reason is a fixed word.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from joshua_shared import config
from joshua_shared.config import Assignments, JoshuaConfig
from joshua_shared.log import get_logger
from pydantic import ValidationError

from joshua_assignments import budget, notify
from joshua_assignments.core_client import CoreClient
from joshua_assignments.runner import Runner, StubRunner
from joshua_assignments.settings import Settings
from joshua_assignments.spec import (
    MAX_GOAL_CHARS,
    MAX_TURNS,
    AssignmentSpec,
    SpecRefused,
    check_duration,
    clamp,
    first_error,
    page_path,
    resolve,
)
from joshua_assignments.store import (
    FINISHED,
    PARKED,
    RESUMABLE,
    STATUSES,
    USAGE_FIELDS,
    Store,
)

logger = get_logger("joshua_assignments.manager")

MAX_LIST = 100
MAX_CONVERSATION_ID = 200


def rejected(reason: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, "message": message, **extra}


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def summary_view(row: dict[str, Any]) -> dict[str, Any]:
    """The short view of an assignment, for a list."""
    spec = row.get("spec") or {}
    goal = str(spec.get("goal") or "")
    return {
        "id": row["id"],
        "person": row["person_id"],
        "status": row["status"],
        "goal": goal.splitlines()[0][:200] if goal else "",
        "model": spec.get("model"),
        "page_path": row.get("page_path"),
        "created_at": _iso(row.get("created_at")),
        "finished_at": _iso(row.get("finished_at")),
    }


def full_view(row: dict[str, Any], checkpoint: dict[str, Any] | None) -> dict[str, Any]:
    """The whole state of an assignment, for ``assignment_status``.

    ``next_step`` comes from the latest checkpoint note: the text after
    ``Next:``, else the whole note. The plan page is the full record.
    """
    spec = row.get("spec") or {}
    return {
        **summary_view(row),
        "goal": spec.get("goal"),
        "model_id": row.get("model"),
        "budget": spec.get("budget"),
        "max_turns": spec.get("max_turns"),
        "working_time": spec.get("working_time"),
        "repos": spec.get("repos") or [],
        "tools": spec.get("tools"),
        "tasks_project": spec.get("tasks_project"),
        "report_to": spec.get("report_to"),
        "dry_run": bool(spec.get("dry_run")),
        "next_step": budget.next_step(checkpoint.get("note") if checkpoint else None),
        "last_checkpoint": (
            {"at": _iso(checkpoint.get("at")), "note": checkpoint.get("note")}
            if checkpoint
            else None
        ),
        "usage": {name: int(row.get(name) or 0) for name in USAGE_FIELDS},
        "turns": row.get("turns") or 0,
        "working_s": row.get("working_s") or 0,
        "open_question": row.get("open_question"),
        "asked_at": _iso(row.get("asked_at")),
        "stopped_at_limits": row.get("stopped_at_limits"),
        "summary": row.get("summary"),
        "follow_ups": row.get("follow_ups"),
        "error": row.get("error"),
        "started_at": _iso(row.get("started_at")),
    }


def _now() -> datetime:
    return datetime.now(UTC)


class Manager:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        load_config: Callable[[], JoshuaConfig] = config.load,
        clock: Callable[[], datetime] = _now,
        stub_delay_s: float | None = 0.0,
        runner: Callable[[Manager], Runner] | None = None,
        core: CoreClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.load_config = load_config
        self.clock = clock
        # ``core`` reads the limits for the check of ``when_unknown`` at start.
        self.core = core
        self.runner: Runner = (
            runner(self) if runner is not None else StubRunner(self, delay_s=stub_delay_s)
        )
        self._dispatch_lock = asyncio.Lock()
        # Why a queued assignment that ran before resumes, when not for an answer.
        self._resume_reason: dict[int, str] = {}

    @property
    def section(self) -> Assignments:
        return self.load_config().assignments

    def is_member(self, person: str) -> bool:
        """True when ``person`` is a member in ``joshua.yaml`` now."""
        found = self.load_config().person(person)
        return found is not None and found.role == "member"

    # --- start and stop ---------------------------------------------------

    async def open(self) -> None:
        """Open the store, put each interrupted assignment back in the queue, and dispatch."""
        await self.store.open()
        requeued = await self.store.requeue_running()
        if requeued:
            logger.info({"message": "requeued after a restart", "assignment_ids": requeued})
        for assignment_id in requeued:
            self._resume_reason[assignment_id] = "restart"
        opener = getattr(self.runner, "open", None)
        if opener is not None:
            await opener()
        await self._sweep()
        await self.dispatch()

    async def _sweep(self) -> None:
        sweep = getattr(self.runner, "sweep", None)
        if sweep is None:
            return
        try:
            await sweep()
        except Exception as exc:  # noqa: BLE001 — a failed sweep never stops the queue
            logger.warning({"message": "scratch sweep failed", "error": type(exc).__name__})

    async def close(self) -> None:
        closer = getattr(self.runner, "close", None)
        if closer is not None:
            await closer()
        await self.store.close()

    async def dispatch(self) -> None:
        """Start queued assignments while a slot is free."""
        async with self._dispatch_lock:
            while True:
                counts = await self.store.counts()
                if counts.get("running", 0) >= self.section.max_concurrent:
                    return
                row = await self.store.next_queued()
                if row is None:
                    return
                fields: dict[str, Any] = {"status": "running"}
                resume = row.get("started_at") is not None
                if not resume:
                    fields["started_at"] = self.clock()
                row = await self.store.update(row["id"], **fields)
                assert row is not None
                logger.info(
                    {"message": "assignment running", "assignment_id": row["id"], "resume": resume}
                )
                reason = self._resume_reason.pop(row["id"], None)
                if resume:
                    answer = row.get("answer") or ""
                    await self.runner.resume(
                        row, answer, reason="answer" if answer else (reason or "answer")
                    )
                else:
                    await self.runner.start(row)

    async def stopped(self, assignment_id: int) -> None:
        """The runner stopped an assignment: send its event, then start the next one."""
        row = await self.store.get(assignment_id)
        if row is not None:
            if row["status"] == "paused_limits" and row.get("stopped_at_limits"):
                text = budget.limits_stop_text(
                    row["stopped_at_limits"], row.get("page_path"), self.load_config().timezone
                )
                await notify.send_question(self.settings, self.section, row, text)
            elif row["status"] in PARKED:
                question = row.get("open_question") or "The assignment stopped."
                await notify.send_question(self.settings, self.section, row, question)
            elif row["status"] in FINISHED:
                await notify.send_report(self.settings, self.section, row)
        await self._sweep()
        await self.dispatch()

    # --- the tools --------------------------------------------------------

    async def start(
        self, person: str, raw_spec: Any, conversation_id: str | None = None
    ) -> dict[str, Any]:
        section = self.section
        if not section.enabled:
            return rejected("disabled", "assignments.enabled is false in joshua.yaml")
        if not isinstance(raw_spec, dict):
            return rejected("invalid_spec", "spec must be an object with at least a goal")
        try:
            spec = AssignmentSpec.model_validate(raw_spec)
        except ValidationError as exc:
            return rejected("invalid_spec", first_error(exc))
        try:
            resolved = resolve(spec, section)
        except SpecRefused as exc:
            return rejected(exc.reason, exc.message)
        refused = await self._refuse_unknown_limits(resolved.spec)
        if refused is not None:
            return refused
        if conversation_id:
            conversation_id = conversation_id[:MAX_CONVERSATION_ID]
        row = await self.store.create(
            person_id=person,
            conversation_id=conversation_id or None,
            spec=resolved.spec,
            model=resolved.model_id,
            page_path=lambda assignment_id: page_path(assignment_id, spec.goal),
        )
        logger.info(
            {
                "message": "assignment stored",
                "assignment_id": row["id"],
                "person": person,
                "model": resolved.spec["model"],
                "clamped": len(resolved.notes),
            }
        )
        await self.dispatch()
        current = await self.store.get(row["id"]) or row
        message = f"Assignment {row['id']} is {current['status']}."
        if resolved.notes:
            message += " " + "; ".join(resolved.notes) + "."
        return {
            "id": row["id"],
            "status": current["status"],
            "page_path": row["page_path"],
            "model": resolved.spec["model"],
            "budget": resolved.spec["budget"],
            "max_turns": resolved.spec["max_turns"],
            "working_time": resolved.spec["working_time"],
            "notes": resolved.notes,
            "message": message,
        }

    async def _refuse_unknown_limits(self, spec: dict[str, Any]) -> dict[str, Any] | None:
        """The refusal of ``when_unknown: refuse`` for a spec with no token cap, or None."""
        if self.core is None or self.section.limits.when_unknown != "refuse":
            return None
        if (spec.get("budget") or {}).get("tokens"):
            return None
        check = budget.check_limits(await self.core.limits(), budget.Ceilings.from_spec(spec))
        if not check.is_unknown:
            return None
        return rejected(
            "limits_unknown",
            "core cannot say how much of the subscription limits is used, and "
            "assignments.limits.when_unknown is refuse; give budget.tokens, or try later",
        )

    async def status(self, row: dict[str, Any]) -> dict[str, Any]:
        checkpoint = await self.store.last_checkpoint(row["id"])
        return full_view(row, checkpoint)

    async def recent(self, person: str | None, limit: int, status: str | None) -> dict[str, Any]:
        if status is not None and status not in STATUSES:
            return rejected("invalid_status", f"status is one of {', '.join(STATUSES)}")
        limit = max(1, min(int(limit), MAX_LIST))
        rows = await self.store.recent(person, limit, status)
        return {"assignments": [summary_view(row) for row in rows]}

    async def answer(self, row: dict[str, Any], text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return rejected("invalid_arguments", "text is empty")
        if len(text) > MAX_GOAL_CHARS:
            return rejected("invalid_arguments", f"text is longer than {MAX_GOAL_CHARS} characters")
        if row["status"] not in PARKED:
            return rejected(
                "not_waiting",
                f"assignment {row['id']} is {row['status']}; only a waiting or "
                "paused_limits assignment takes an answer",
            )
        # An answer that is stored and not sent yet stays: the new text follows it.
        earlier = (row.get("answer") or "").strip()
        if earlier and earlier != text:
            text = f"{earlier}\n{text}"[-MAX_GOAL_CHARS:]
        over = await self._still_over(row)
        if over is not None:
            await self.store.update(row["id"], answer=text)
            logger.info({"message": "answer stored; over a ceiling", "assignment_id": row["id"]})
            return {
                "id": row["id"],
                "status": row["status"],
                "message": f"Assignment {row['id']} stays {row['status']}. {over} "
                "The answer is stored. Answer again when the window resets, or raise the "
                "ceiling with extend_assignment.",
            }
        await self.store.update(
            row["id"], answer=text, open_question=None, stopped_at_limits=None, status="queued"
        )
        logger.info({"message": "assignment answered", "assignment_id": row["id"]})
        await self.dispatch()
        current = await self.store.get(row["id"]) or row
        return {
            "id": row["id"],
            "status": current["status"],
            "message": f"The answer is stored. Assignment {row['id']} is {current['status']}.",
        }

    async def _still_over(self, row: dict[str, Any]) -> str | None:
        """Why the limits keep the assignment parked, or None when it may run.

        It reads core's limits now. A window at or over its ceiling keeps it
        parked. Unknown limits keep it parked when ``when_unknown`` is
        ``refuse`` and the spec has no token cap.
        """
        if self.core is None:
            return None
        spec = row.get("spec") or {}
        ceilings = budget.Ceilings.from_spec(spec)
        check = budget.check_limits(await self.core.limits(), ceilings)
        if check.over is not None:
            window = check.windows[check.over]
            text = (
                f"The {budget.LABELS[check.over]} is at {budget.number_text(window['used_pct'])} "
                f"percent, and its ceiling is {ceilings.of(check.over)} percent."
            )
            when = budget.when_text(window.get("resets_at"), self.load_config().timezone)
            return f"{text} It resets at {when}." if when else text
        tokens = (spec.get("budget") or {}).get("tokens")
        if budget.blocked_when_unknown(check, self.section.limits.when_unknown, tokens):
            return "Core cannot say how much of the subscription limits is used."
        return None

    async def cancel(self, row: dict[str, Any]) -> dict[str, Any]:
        if row["status"] in FINISHED:
            return rejected("finished", f"assignment {row['id']} has ended: {row['status']}")
        await self.runner.cancel(row)
        await self.store.update(row["id"], status="cancelled", finished_at=self.clock())
        logger.info({"message": "assignment cancelled", "assignment_id": row["id"]})
        await self.dispatch()
        return {
            "id": row["id"],
            "status": "cancelled",
            "page_path": row.get("page_path"),
            "message": f"Assignment {row['id']} is cancelled. The row and the plan page stay.",
        }

    async def extend(
        self,
        row: dict[str, Any],
        *,
        tokens: int | None = None,
        working_time: str | None = None,
        five_hour_pct: int | None = None,
        seven_day_pct: int | None = None,
        max_turns: int | None = None,
    ) -> dict[str, Any]:
        """Raise caps. A ``budget_exceeded`` or ``time_exceeded`` assignment resumes."""
        if row["status"] in FINISHED and row["status"] not in RESUMABLE:
            return rejected("finished", f"assignment {row['id']} has ended: {row['status']}")
        caps = (tokens, working_time, five_hour_pct, seven_day_pct, max_turns)
        if all(value is None for value in caps):
            return rejected("invalid_arguments", "give at least one cap to raise")
        section = self.section
        spec = dict(row.get("spec") or {})
        budget = dict(spec.get("budget") or {})
        notes: list[str] = []
        if tokens is not None:
            current = budget.get("tokens")
            if tokens <= 0 or (current is not None and tokens <= current):
                return rejected("not_raised", "tokens must be more than the current cap")
            budget["tokens"] = tokens
        if max_turns is not None:
            current_turns = int(spec.get("max_turns") or section.default_max_turns)
            if max_turns <= current_turns or max_turns > MAX_TURNS:
                return rejected(
                    "not_raised",
                    f"max_turns must be more than {current_turns} and at most {MAX_TURNS}",
                )
            spec["max_turns"] = max_turns
        if working_time is not None:
            try:
                new_s = check_duration(working_time)
            except ValueError as exc:
                return rejected("invalid_arguments", f"working_time: {exc}")
            if new_s <= check_duration(spec.get("working_time") or section.default_working_time):
                return rejected("not_raised", "working_time must be more than the current cap")
            spec["working_time"] = working_time
        for name, value, maximum in (
            ("five_hour_pct", five_hour_pct, section.limits.max_five_hour_pct),
            ("seven_day_pct", seven_day_pct, section.limits.max_seven_day_pct),
        ):
            if value is None:
                continue
            value = clamp(f"budget.{name}", value, maximum, notes)
            if value <= (budget.get(name) or 0):
                return rejected(
                    "not_raised", f"budget.{name} must be more than the current {budget.get(name)}"
                )
            budget[name] = value
        spec["budget"] = budget
        resume = row["status"] in RESUMABLE
        if resume:
            await self.store.update(
                row["id"], spec=spec, status="queued", finished_at=None, error=None
            )
            self._resume_reason[row["id"]] = "extend"
        else:
            await self.store.update(row["id"], spec=spec)
        logger.info(
            {"message": "assignment extended", "assignment_id": row["id"], "resume": resume}
        )
        if resume:
            await self.dispatch()
        current = await self.store.get(row["id"]) or row
        message = f"Assignment {row['id']} has new caps."
        if resume:
            message += f" It continues its session: {current['status']}."
        if notes:
            message += " " + "; ".join(notes) + "."
        return {
            "id": row["id"],
            "status": current["status"],
            "max_turns": spec.get("max_turns"),
            "budget": budget,
            "working_time": spec.get("working_time"),
            "notes": notes,
            "message": message,
        }
