"""The manager: the rules behind the tools.

The manager validates a spec and stores the assignment. ``draft`` stores it
as a ``draft``, which never runs until ``start`` starts it. ``start`` takes
a slot at once, or refuses with the reason ``busy``: there is no queue, and
nothing waits for a slot. ``assignments.max_concurrent`` counts ``running``
assignments only, so a parked assignment holds no slot. When the runner
stops an assignment, ``stopped`` sends the report or the question to
channels. It starts nothing.

A parked assignment (``waiting`` or ``paused_limits``) waits with no time
limit. Only ``answer_assignment`` starts it again, and only when the limits
are under the ceilings and a slot is free. ``extend_assignment`` starts a
``budget_exceeded`` or ``time_exceeded`` assignment again when a slot is
free. At start, each ``running`` assignment resumes its session with a note
that the container restarted.

A worker can park with a request for a repository or a file.
``grant_repo`` adds a repository that ``assignments.repos`` allows, and
``provide_file`` writes a text file to ``inbox/`` of the scratch directory
(see ``inbox``). Each resumes the assignment as ``answer_assignment`` does,
with the same ``busy`` refusal, and clears the open request.

A draft takes a new spec with ``update_draft``. ``delete_draft`` only
removes it from view (``deleted_at``), and ``restore`` brings it back. A row
that ran is never deleted. ``start`` with a new spec is refused as
``drafts_open`` while the person has a draft, unless the caller says that the
job is ``new``: a draft is started by its id, and keeps that id when it runs.

``cancel`` holds the slot lock until the session has stopped and the row is
``cancelled``, so a start right after it is never refused as ``busy``.

A saved assignment is a spec with a name and no run. ``start`` with
``saved`` takes the stored spec as the base, and a ``spec`` beside it
replaces the top-level fields that it gives. The run keeps the id of the
saved assignment.

A rejected call returns ``{"status": "rejected", "reason", "message"}``. The
reason is a fixed word. A refusal of ``start`` also has ``hint``, one
sentence that Joshua can tell the person, and where it applies
``retry_after_s``, ``running`` (``{"id", "title"}``; another person's run has
no id), or ``drafts``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from joshua_shared import config
from joshua_shared.config import Assignments, JoshuaConfig, parse_duration
from joshua_shared.log import get_logger
from pydantic import ValidationError

from joshua_assignments import budget, inbox, notify, prompts, report
from joshua_assignments import definition as definition_mod
from joshua_assignments.comments import CommentRefused, comment_view, resolution_text
from joshua_assignments.core_client import CoreClient
from joshua_assignments.repos import in_scope
from joshua_assignments.runner import Runner, StubRunner
from joshua_assignments.settings import Settings
from joshua_assignments.spec import (
    MAX_GOAL_CHARS,
    MAX_TURNS,
    AssignmentSpec,
    Resolved,
    SpecRefused,
    assignment_title,
    check_duration,
    check_repo,
    clamp,
    first_error,
    resolve,
    saved_name,
)
from joshua_assignments.store import (
    DRAFT,
    FINISHED,
    PARKED,
    RESUMABLE,
    STATES,
    STATUSES,
    USAGE_FIELDS,
    Parent,
    Store,
    state,
    statuses_of,
)

logger = get_logger("joshua_assignments.manager")

MAX_LIST = 100
MAX_CONVERSATION_ID = 200
# The most drafts that a ``drafts_open`` refusal names.
MAX_OPEN_DRAFTS = 20
# ``retry_after_s`` of a ``busy`` refusal: the working time that the running
# assignment has left, in these bounds. Another person's run gives the default.
BUSY_RETRY_MIN_S = 300
BUSY_RETRY_MAX_S = 3600
BUSY_RETRY_DEFAULT_S = 900
# ``retry_after_s`` of a ``limits_unknown`` refusal.
LIMITS_RETRY_S = 600
# What a refusal of ``busy`` calls a run of another person.
OTHER_RUN = "another person's assignment"

# The ``hint`` of a refusal of ``start`` that does not give its own.
START_HINTS = {
    "disabled": "Assignments are off on this instance, so nothing can start.",
    "invalid_arguments": (
        "Give exactly one of id (a draft), saved (the name of a saved assignment), "
        "or spec (a new job)."
    ),
    "not_found": "There is no saved assignment with that name; list_saved_assignments "
    "gives the names.",
}


def rejected(reason: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, "message": message, **extra}


def with_hint(reply: dict[str, Any]) -> dict[str, Any]:
    """``reply``, and when it is a refusal with no ``hint``, the hint of its reason.

    A reason with no fixed hint (a spec that is refused) gets its message as
    the hint."""
    if reply.get("status") != "rejected" or reply.get("hint"):
        return reply
    message = str(reply.get("message") or "")
    fallback = f"The spec is refused: {message}. Change the spec, then start it again."
    reply["hint"] = START_HINTS.get(str(reply.get("reason")), fallback)
    return reply


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def saved_ref(row: dict[str, Any], names: Mapping[int, str]) -> dict[str, Any] | None:
    """``{"id", "name"}`` of the saved assignment that the run started from, or None."""
    saved_id = row.get("saved_id")
    if saved_id is None or saved_id not in names:
        return None
    return {"id": int(saved_id), "name": names[saved_id]}


def summary_view(row: dict[str, Any], names: Mapping[int, str] | None = None) -> dict[str, Any]:
    """The short view of an assignment, for a list. ``names`` names the saved assignments."""
    spec = row.get("spec") or {}
    goal = str(spec.get("goal") or "")
    return {
        "id": row["id"],
        "person": row["person_id"],
        "title": assignment_title(spec),
        "status": row["status"],
        "state": state(row["status"]),
        "goal": goal.splitlines()[0][:200] if goal else "",
        "model": spec.get("model"),
        "created_at": _iso(row.get("created_at")),
        "finished_at": _iso(row.get("finished_at")),
        "saved": saved_ref(row, names or {}),
    }


def saved_view(row: dict[str, Any]) -> dict[str, Any]:
    """A saved assignment, for ``list_saved_assignments``."""
    last = row.get("last_run")
    return {
        "id": row["id"],
        "name": row["name"],
        "title": assignment_title(row.get("spec")),
        "runs": int(row.get("runs") or 0),
        "last_run": (
            {"id": last["id"], "status": last["status"], "finished_at": _iso(last["finished_at"])}
            if last
            else None
        ),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def full_view(
    row: dict[str, Any],
    checkpoint: dict[str, Any] | None,
    names: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """The whole state of an assignment, for ``assignment_status``.

    ``next_step`` comes from the latest checkpoint note: the text after
    ``Next:``, else the whole note. ``plan`` is the plan of the worker, and
    ``report`` is the report, with ``outputs`` the recorded outputs.
    """
    spec = row.get("spec") or {}
    return {
        **summary_view(row, names),
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
        "open_request": row.get("open_request"),
        "asked_at": _iso(row.get("asked_at")),
        "granted_repos": row.get("granted_repos") or [],
        "inbox": inbox.records(row.get("inbox")),
        "stopped_at_limits": row.get("stopped_at_limits"),
        "summary": row.get("summary"),
        "follow_ups": row.get("follow_ups"),
        "error": row.get("error"),
        "started_at": _iso(row.get("started_at")),
        "plan": row.get("plan"),
        "plan_at": _iso(row.get("plan_at")),
        "report": row.get("report"),
        "report_at": _iso(row.get("report_at")),
        "outputs": row.get("outputs") or [],
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
        # Held while a call counts the running assignments and takes a slot.
        self._slot_lock = asyncio.Lock()

    @property
    def section(self) -> Assignments:
        return self.load_config().assignments

    def is_member(self, person: str) -> bool:
        """True when ``person`` is a member in ``joshua.yaml`` now."""
        found = self.load_config().person(person)
        return found is not None and found.role == "member"

    # --- start and stop ---------------------------------------------------

    async def open(self) -> None:
        """Open the store, then resume each assignment that ran when the container stopped."""
        await self.store.open()
        opener = getattr(self.runner, "open", None)
        if opener is not None:
            await opener()
        await self._sweep()
        async with self._slot_lock:
            rows = await self.store.running()
            if rows:
                logger.info(
                    {
                        "message": "resumed after a restart",
                        "assignment_ids": [r["id"] for r in rows],
                    }
                )
            for row in rows:
                await self._run(row, "restart")

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

    async def _busy(
        self, person: str, then: str = "start this one when it ends, do not create another"
    ) -> dict[str, Any] | None:
        """The ``busy`` refusal when every slot is taken, else None.

        ``running`` is ``{"id", "title"}`` of the newest running assignment of
        ``person``. When only assignments of other people run, it has no id,
        and its title is :data:`OTHER_RUN`. ``then`` ends the hint: what to do
        when the slot is free. The caller holds the slot lock."""
        rows = await self.store.running()
        section = self.section
        maximum = section.max_concurrent
        if len(rows) < maximum:
            return None
        mine = [row for row in rows if row["person_id"] == person]
        if mine:
            row = mine[-1]
            name = f"assignment {row['id']}"
            running: dict[str, Any] = {"id": int(row["id"]), "title": assignment_title(row["spec"])}
            spec = row.get("spec") or {}
            try:
                working_time = spec.get("working_time") or section.default_working_time
                total = parse_duration(str(working_time))
            except ValueError:
                total = BUSY_RETRY_DEFAULT_S
            left = total - int(row.get("working_s") or 0)
            retry = max(BUSY_RETRY_MIN_S, min(BUSY_RETRY_MAX_S, left))
        else:
            name = OTHER_RUN
            running = {"id": None, "title": OTHER_RUN}
            retry = BUSY_RETRY_DEFAULT_S
        return rejected(
            "busy",
            f"{name} is running, and assignments.max_concurrent is {maximum}. There is "
            "no queue: nothing waits for the slot",
            hint=f"{name[0].upper()}{name[1:]} is still running; {then}.",
            retry_after_s=retry,
            running=running,
        )

    async def _run(self, row: dict[str, Any], reason: str | None, **fields: Any) -> dict[str, Any]:
        """Set ``row`` running with ``fields`` and give it to the runner.

        A row that never started gets ``started_at`` and a new session. A row
        that ran before resumes its session: for its stored answer when it
        has one, else for ``reason``. The caller holds the slot lock."""
        resume = row.get("started_at") is not None
        fields["status"] = "running"
        if not resume:
            fields["started_at"] = self.clock()
        current = await self.store.update(row["id"], **fields)
        assert current is not None
        logger.info(
            {"message": "assignment running", "assignment_id": current["id"], "resume": resume}
        )
        if resume:
            answer = current.get("answer") or ""
            await self.runner.resume(
                current, answer, reason="answer" if answer else (reason or "answer")
            )
        else:
            await self.runner.start(current)
        return current

    async def stopped(self, assignment_id: int) -> None:
        """The runner stopped an assignment: send its event. Nothing else starts."""
        row = await self.store.get(assignment_id)
        if row is not None:
            if row["status"] == "paused_limits" and row.get("stopped_at_limits"):
                text = budget.limits_stop_text(
                    row["stopped_at_limits"], self.load_config().timezone
                )
                await notify.send_question(self.settings, self.section, row, text)
            elif row["status"] in PARKED:
                question = row.get("open_question") or "The assignment stopped."
                await notify.send_question(self.settings, self.section, row, question)
            elif row["status"] in FINISHED:
                await notify.send_report(self.settings, self.section, row)
        await self._sweep()

    # --- the tools --------------------------------------------------------

    def _check(self, raw_spec: Any) -> tuple[AssignmentSpec, Resolved] | dict[str, Any]:
        """The checked spec and its resolved form, or the rejection."""
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
            return spec, resolve(spec, section)
        except SpecRefused as exc:
            return rejected(exc.reason, exc.message)

    async def _spec_of(
        self, person: str, raw_spec: Any, saved: str | None
    ) -> tuple[Any, dict[str, Any] | None] | dict[str, Any]:
        """The raw spec and the saved assignment it comes from, or the rejection.

        With ``saved``, the stored spec is the base, and ``raw_spec``, when
        given, replaces the top-level fields that it gives."""
        if saved is None:
            if raw_spec is None:
                return rejected("invalid_arguments", "give spec, or the name of a saved assignment")
            return raw_spec, None
        if raw_spec is not None and not isinstance(raw_spec, dict):
            return rejected("invalid_spec", "spec must be an object")
        try:
            name = saved_name(saved)
        except ValueError as exc:
            return rejected("invalid_arguments", str(exc))
        source = await self.store.find_saved(person, name)
        if source is None:
            return rejected("not_found", f"there is no saved assignment {name}")
        return {**source["spec"], **(raw_spec or {})}, source

    @staticmethod
    def _reply(
        row: dict[str, Any],
        spec: dict[str, Any],
        notes: list[str],
        source: dict[str, Any] | None,
        message: str,
    ) -> dict[str, Any]:
        if notes:
            message += " " + "; ".join(notes) + "."
        return {
            "id": row["id"],
            "status": row["status"],
            "title": spec["title"],
            "model": spec["model"],
            "budget": spec["budget"],
            "max_turns": spec["max_turns"],
            "working_time": spec["working_time"],
            "notes": notes,
            "saved": {"id": source["id"], "name": source["name"]} if source else None,
            "message": message,
        }

    async def start(
        self,
        person: str,
        raw_spec: Any = None,
        conversation_id: str | None = None,
        saved: str | None = None,
        draft: dict[str, Any] | None = None,
        new: bool = False,
    ) -> dict[str, Any]:
        """Start an assignment now: the draft ``draft`` (a row of ``person``), a
        new one from the saved assignment ``saved``, or a new one from
        ``raw_spec``.

        With ``saved``, ``raw_spec`` is optional and replaces the top-level
        fields of the stored spec that it gives. A ``raw_spec`` alone is
        refused as ``drafts_open`` while the person has a draft, unless
        ``new`` is true. When every slot is taken, the start is refused with
        ``busy`` and nothing is stored. Every refusal has a ``hint``.
        """
        return with_hint(
            await self._start(person, raw_spec, conversation_id, saved, draft, new=new)
        )

    async def _start(
        self,
        person: str,
        raw_spec: Any,
        conversation_id: str | None,
        saved: str | None,
        draft: dict[str, Any] | None,
        *,
        new: bool,
    ) -> dict[str, Any]:
        if not self.section.enabled:
            return rejected("disabled", "assignments.enabled is false in joshua.yaml")
        if draft is not None:
            if raw_spec is not None or saved is not None:
                return rejected("invalid_arguments", "give one of spec, id, or saved")
            return await self._start_draft(person, draft)
        if saved is None and raw_spec is not None and not new:
            refused = await self._drafts_open(person)
            if refused is not None:
                return refused
        found = await self._spec_of(person, raw_spec, saved)
        if isinstance(found, dict):
            return found
        raw_spec, source = found
        checked = self._check(raw_spec)
        if isinstance(checked, dict):
            return checked
        _, resolved = checked
        refused = await self._refuse_unknown_limits(resolved.spec)
        if refused is not None:
            return refused
        if conversation_id:
            conversation_id = conversation_id[:MAX_CONVERSATION_ID]
        async with self._slot_lock:
            busy = await self._busy(person)
            if busy is not None:
                return busy
            row = await self.store.create(
                person_id=person,
                conversation_id=conversation_id or None,
                spec=resolved.spec,
                model=resolved.model_id,
                saved_id=source["id"] if source else None,
            )
            logger.info(
                {
                    "message": "assignment stored",
                    "assignment_id": row["id"],
                    "person": person,
                    "model": resolved.spec["model"],
                    "clamped": len(resolved.notes),
                    "saved_id": source["id"] if source else None,
                }
            )
            current = await self._run(row, None)
        message = f"Assignment {row['id']} is {current['status']}."
        if source:
            message += f" It started from the saved assignment {source['name']}."
        return self._reply(current, resolved.spec, resolved.notes, source, message)

    async def _drafts_open(self, person: str) -> dict[str, Any] | None:
        """The ``drafts_open`` refusal when ``person`` has a draft, else None."""
        rows = await self.store.recent(person, MAX_OPEN_DRAFTS, statuses=(DRAFT,))
        if not rows:
            return None
        drafts = [{"id": int(row["id"]), "title": assignment_title(row["spec"])} for row in rows]
        names = ", ".join(f"{d['id']} ({d['title']})" for d in drafts)
        return rejected(
            "drafts_open",
            f"the person has drafts: {names}. A new spec is refused while a draft "
            "can cover the job",
            hint="Start the draft by id, or pass new: true for a job none of these covers.",
            drafts=drafts,
        )

    async def _start_draft(self, person: str, row: dict[str, Any]) -> dict[str, Any]:
        """Start the draft ``row`` as itself: the run keeps the id of the draft.

        Its spec is checked again, as the config can change."""
        if row.get("deleted_at") is not None:
            return rejected(
                "deleted",
                f"assignment {row['id']} is a deleted draft",
                hint=f"Draft {row['id']} is deleted. When the person wants it, bring it back "
                f"with restore_assignment({row['id']}), then start it.",
            )
        if row["status"] != DRAFT:
            return self._not_a_draft(row)
        checked = self._check(row.get("spec") or {})
        if isinstance(checked, dict):
            return checked
        _, resolved = checked
        refused = await self._refuse_unknown_limits(resolved.spec)
        if refused is not None:
            return refused
        async with self._slot_lock:
            busy = await self._busy(person)
            if busy is not None:
                return busy
            current = await self.store.get(row["id"])
            if current is None or current["status"] != DRAFT:
                return self._not_a_draft(current or row)
            current = await self._run(
                current, "restart", spec=resolved.spec, model=resolved.model_id
            )
        names = await self._names([current])
        source = saved_ref(current, names)
        return self._reply(
            current,
            resolved.spec,
            resolved.notes,
            source,
            f"Assignment {row['id']} is {current['status']}.",
        )

    @staticmethod
    def _not_a_draft(row: dict[str, Any]) -> dict[str, Any]:
        return rejected(
            "not_a_draft",
            f"assignment {row['id']} is {row['status']}; only a draft takes this call",
            hint=f"Assignment {row['id']} is {row['status']}, not a draft, so it does not "
            f"start again; assignment_status({row['id']}) says what it did.",
        )

    async def draft(
        self,
        person: str,
        raw_spec: Any = None,
        conversation_id: str | None = None,
        saved: str | None = None,
    ) -> dict[str, Any]:
        """Check a spec as ``start`` does and store it as a draft. Nothing runs.

        ``saved`` and ``raw_spec`` work as in ``start``."""
        if not self.section.enabled:
            return rejected("disabled", "assignments.enabled is false in joshua.yaml")
        found = await self._spec_of(person, raw_spec, saved)
        if isinstance(found, dict):
            return found
        raw_spec, source = found
        checked = self._check(raw_spec)
        if isinstance(checked, dict):
            return checked
        _, resolved = checked
        if conversation_id:
            conversation_id = conversation_id[:MAX_CONVERSATION_ID]
        row = await self.store.create(
            person_id=person,
            conversation_id=conversation_id or None,
            spec=resolved.spec,
            model=resolved.model_id,
            status=DRAFT,
            saved_id=source["id"] if source else None,
        )
        logger.info({"message": "draft stored", "assignment_id": row["id"], "person": person})
        message = (
            f"Assignment {row['id']} is a draft. Nothing runs until "
            f"start_assignment(id={row['id']})."
        )
        return self._reply(row, resolved.spec, resolved.notes, source, message)

    async def update_draft(self, row: dict[str, Any], raw_spec: Any) -> dict[str, Any]:
        """Replace the spec of the draft ``row``. Any other status is refused."""
        if not self.section.enabled:
            return rejected("disabled", "assignments.enabled is false in joshua.yaml")
        if row["status"] != DRAFT:
            return self._not_a_draft(row)
        checked = self._check(raw_spec)
        if isinstance(checked, dict):
            return checked
        _, resolved = checked
        current = await self.store.update(row["id"], spec=resolved.spec, model=resolved.model_id)
        assert current is not None
        lines = definition_mod.lines(resolved.spec)
        person = row["person_id"]
        comments = await self.store.comments(
            person, Parent("assignment", int(row["id"])), open_only=True
        )
        stale = sum(1 for comment in comments if comment_view(comment, lines)["stale"])
        logger.info({"message": "draft updated", "assignment_id": row["id"], "person": person})
        names = await self._names([current])
        message = f"Assignment {row['id']} has the new spec. It is still a draft."
        if stale:
            message += f" {stale} open comment" + (" is" if stale == 1 else "s are") + " stale."
        reply = self._reply(
            current, resolved.spec, resolved.notes, saved_ref(current, names), message
        )
        reply["open_comments"] = len(comments)
        reply["stale_comments"] = stale
        return reply

    async def delete_draft(self, row: dict[str, Any]) -> dict[str, Any]:
        """Remove the draft ``row`` from view. A row that ran is never deleted.

        The row and its comments stay, and ``restore`` brings them back."""
        if row["status"] != DRAFT or not await self.store.delete_draft(int(row["id"])):
            return rejected(
                "not_a_draft",
                f"assignment {row['id']} is {row['status']}; only a draft that never ran "
                "is deleted, and a run stays",
            )
        logger.info({"message": "draft deleted", "assignment_id": row["id"]})
        return {
            "id": row["id"],
            "status": "deleted",
            "message": f"The draft {row['id']} is removed from view. When the person asks "
            f"for it, restore_assignment({row['id']}) brings it back as a draft.",
        }

    async def restore(self, row: dict[str, Any]) -> dict[str, Any]:
        """Bring back the deleted draft ``row`` as a draft, with its comments."""
        if row.get("deleted_at") is None:
            return rejected(
                "not_deleted", f"assignment {row['id']} is not deleted: it is {row['status']}"
            )
        current = await self.store.restore_draft(int(row["id"]))
        if current is None:
            return rejected("not_deleted", f"assignment {row['id']} is not a deleted draft")
        logger.info({"message": "draft restored", "assignment_id": row["id"]})
        return {
            "id": current["id"],
            "status": current["status"],
            "title": assignment_title(current["spec"]),
            "message": f"Draft {current['id']} is back. Start it with "
            f"start_assignment(id={current['id']}).",
        }

    async def save(self, person: str, raw_name: Any, raw_spec: Any) -> dict[str, Any]:
        """Check ``raw_spec`` as ``start`` does, then store it as the saved assignment ``name``.

        A saved assignment of the person with the same name gets the new spec.
        The store keeps the spec as given, so a default that changes later
        applies at the next run.
        """
        try:
            name = saved_name(raw_name)
        except ValueError as exc:
            return rejected("invalid_arguments", str(exc))
        checked = self._check(raw_spec)
        if isinstance(checked, dict):
            return checked
        spec, resolved = checked
        given = spec.model_dump(mode="json", exclude_unset=True)
        row = await self.store.put_saved(person, name, given)
        logger.info(
            {
                "message": "saved assignment stored",
                "saved_id": row["id"],
                "person": person,
                "created": bool(row.get("created")),
            }
        )
        verb = "is saved" if row.get("created") else "has a new spec"
        message = (
            f"The saved assignment {name} {verb}. Start it with start_assignment(saved={name!r})."
        )
        if resolved.notes:
            message += " At a run, " + "; ".join(resolved.notes) + "."
        return {
            "id": row["id"],
            "name": name,
            "title": assignment_title(given),
            "notes": resolved.notes,
            "message": message,
        }

    async def list_saved(self, person: str) -> dict[str, Any]:
        rows = await self.store.list_saved(person)
        return {"saved": [saved_view(row) for row in rows]}

    async def delete_saved(self, person: str, raw_name: Any) -> dict[str, Any]:
        try:
            name = saved_name(raw_name)
        except ValueError as exc:
            return rejected("invalid_arguments", str(exc))
        if not await self.store.delete_saved(person, name):
            return rejected("not_found", f"there is no saved assignment {name}")
        logger.info({"message": "saved assignment deleted", "person": person})
        return {
            "name": name,
            "status": "deleted",
            "message": f"The saved assignment {name} is deleted. Its runs stay.",
        }

    async def open_comments(
        self, person: str, *, saved: Any = None, row: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """The open comments on the definition of the saved assignment ``saved``
        (a name) or of the run ``row``, each with its line now.

        ``row`` is a run of ``person`` that the caller found. The answer also
        has the spec and the definition, so the changed spec can be built
        from it."""
        if (saved is None) == (row is None):
            return rejected("invalid_arguments", "give saved (a name) or id, not both")
        source: dict[str, Any] | None = row
        if saved is not None:
            try:
                name = saved_name(saved)
            except ValueError as exc:
                return rejected("invalid_arguments", str(exc))
            source = await self.store.find_saved(person, name)
            if source is None:
                return rejected("not_found", f"there is no saved assignment {name}")
            parent = Parent("saved", int(source["id"]))
        else:
            assert row is not None
            parent = Parent("assignment", int(row["id"]))
        assert source is not None
        spec = source.get("spec") or {}
        lines = definition_mod.lines(spec)
        rows = await self.store.comments(person, parent, open_only=True)
        items = []
        for comment in rows:
            view = comment_view(comment, lines)
            items.append(
                {
                    "id": view["id"],
                    "line_no": view["line_no"],
                    "line_text": view["line_text"],
                    "line_now": definition_mod.line_at(lines, view["line_no"]),
                    "stale": view["stale"],
                    "body": view["body"],
                    "created_at": view["created_at"],
                }
            )
        if parent.kind == "saved":
            how = (
                f"Apply each comment: change the spec, save it with "
                f"save_assignment({source['name']!r}, spec)"
            )
        elif source.get("status") == DRAFT:
            how = (
                f"Apply each comment: change the spec, give it to the draft with "
                f"update_assignment({parent.id}, spec)"
            )
        else:
            how = "Apply each comment: change the spec and start a new run with it"
        message = (
            f"{len(items)} open comment" + ("" if len(items) == 1 else "s") + ". "
            "Each body is the request of the person, on the line that line_text "
            f"gives. {how}, then call resolve_comment(comment_id, resolution) for each."
            if items
            else "No open comment."
        )
        return {
            "saved": {"id": source["id"], "name": source["name"]}
            if parent.kind == "saved"
            else None,
            "id": parent.id if parent.kind == "assignment" else None,
            "spec": spec,
            "definition": "\n".join(lines),
            "comments": items,
            "message": message,
        }

    async def resolve_comment(
        self, person: str, comment_id: Any, resolution: Any
    ) -> dict[str, Any]:
        """Mark one open comment of ``person`` resolved, with one line on what was done."""
        if isinstance(comment_id, bool) or not isinstance(comment_id, int):
            return rejected("invalid_arguments", "comment_id is a number")
        try:
            text = resolution_text(resolution)
        except CommentRefused as exc:
            return rejected("invalid_arguments", str(exc))
        current = await self.store.get_comment(person, comment_id)
        if current is None:
            return rejected("not_found", f"there is no comment {comment_id}")
        if current.get("resolved_at") is not None:
            return rejected("resolved", f"comment {comment_id} is already resolved")
        row = await self.store.resolve_comment(person, comment_id, text)
        if row is None:
            return rejected("resolved", f"comment {comment_id} is already resolved")
        logger.info({"message": "comment resolved", "comment_id": comment_id, "person": person})
        return {
            "id": comment_id,
            "status": "resolved",
            "resolution": text,
            "message": f"Comment {comment_id} is resolved.",
        }

    async def _names(self, rows: list[dict[str, Any]]) -> dict[int, str]:
        """The names of the saved assignments that ``rows`` started from."""
        ids = {int(row["saved_id"]) for row in rows if row.get("saved_id") is not None}
        return await self.store.saved_names(ids)

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
            hint="Joshua cannot read the subscription limits now; start it again in a few "
            "minutes, or give it a token cap.",
            retry_after_s=LIMITS_RETRY_S,
        )

    async def status(self, row: dict[str, Any]) -> dict[str, Any]:
        checkpoint = await self.store.last_checkpoint(row["id"])
        return full_view(row, checkpoint, await self._names([row]))

    async def recent(
        self, person: str | None, limit: int, status: str | None, state: str | None = None
    ) -> dict[str, Any]:
        if status is not None and status not in STATUSES:
            return rejected("invalid_status", f"status is one of {', '.join(STATUSES)}")
        if state is not None and state not in STATES:
            return rejected("invalid_state", f"state is one of {', '.join(STATES)}")
        limit = max(1, min(int(limit), MAX_LIST))
        statuses = statuses_of(state) if state is not None else None
        rows = await self.store.recent(person, limit, status, statuses)
        names = await self._names(rows)
        return {"assignments": [summary_view(row, names) for row in rows]}

    @staticmethod
    def _not_waiting(row: dict[str, Any]) -> dict[str, Any] | None:
        if row["status"] in PARKED:
            return None
        return rejected(
            "not_waiting",
            f"assignment {row['id']} is {row['status']}; only a waiting or "
            "paused_limits assignment takes an answer",
        )

    async def answer(self, row: dict[str, Any], text: str) -> dict[str, Any]:
        """Give a parked assignment its answer and resume it.

        It answers a question, and a request for a repository or a file with
        a refusal or another way."""
        text = (text or "").strip()
        if not text:
            return rejected("invalid_arguments", "text is empty")
        if len(text) > MAX_GOAL_CHARS:
            return rejected("invalid_arguments", f"text is longer than {MAX_GOAL_CHARS} characters")
        refused = self._not_waiting(row)
        if refused is not None:
            return refused
        return await self._resume(row, text, what="answer")

    async def _resume(
        self,
        row: dict[str, Any],
        text: str,
        *,
        what: str,
        stored: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """Resume the parked ``row`` with the message ``text``.

        ``stored`` stores the grant or the file, and returns the row fields it
        changes. It runs only when the assignment is not refused as ``busy``.
        Over a ceiling, the change and the text are stored, and the
        assignment stays parked; the request stays open."""
        # An answer that is stored and not sent yet stays: the new text follows it.
        earlier = (row.get("answer") or "").strip()
        if earlier and earlier != text:
            text = f"{earlier}\n{text}"[-MAX_GOAL_CHARS:]
        over = await self._still_over(row)
        if over is not None:
            fields = await stored() if stored is not None else {}
            await self.store.update(row["id"], answer=text, **fields)
            logger.info({"message": f"{what} stored; over a ceiling", "assignment_id": row["id"]})
            return {
                "id": row["id"],
                "status": row["status"],
                "message": f"Assignment {row['id']} stays {row['status']}. {over} "
                f"The {what} is stored. Answer again when the window resets, or raise the "
                "ceiling with extend_assignment.",
            }
        async with self._slot_lock:
            busy = await self._busy(
                row["person_id"], f"give the {what} to assignment {row['id']} again when it ends"
            )
            if busy is not None:
                busy["message"] = (
                    f"assignment {row['id']} stays {row['status']} and the {what} is not "
                    f"stored: {busy['message']}"
                )
                return busy
            current = await self.store.get(row["id"])
            if current is None or current["status"] not in PARKED:
                return rejected("not_waiting", f"assignment {row['id']} is not parked now")
            fields = await stored() if stored is not None else {}
            current = await self._run(
                current,
                "answer",
                answer=text,
                open_question=None,
                open_request=None,
                stopped_at_limits=None,
                **fields,
            )
        logger.info({"message": f"assignment resumed: {what}", "assignment_id": row["id"]})
        return {
            "id": row["id"],
            "status": current["status"],
            "message": f"The {what} is stored. Assignment {row['id']} is {current['status']}.",
        }

    async def grant_repo(self, row: dict[str, Any], raw_repo: Any) -> dict[str, Any]:
        """Add one repository to the parked ``row`` and resume it with its clone command.

        The repository is checked against ``assignments.repos`` as a repository
        of a spec is, so a grant never goes past the allowlist."""
        if not self.section.enabled:
            return rejected("disabled", "assignments.enabled is false in joshua.yaml")
        refused = self._not_waiting(row)
        if refused is not None:
            return refused
        try:
            repo = check_repo(raw_repo, self.section)
        except SpecRefused as exc:
            return rejected(exc.reason, exc.message)
        granted = [r for r in row.get("granted_repos") or [] if isinstance(r, str)]
        if repo not in granted and repo not in in_scope(row):
            granted.append(repo)
        work = self._work_dir(row["id"]) or Path(str(int(row["id"])))
        text = prompts.granted_text({**row, "granted_repos": granted}, repo, work)

        async def stored() -> dict[str, Any]:
            return {"granted_repos": granted}

        reply = await self._resume(row, text, what="grant", stored=stored)
        if reply.get("status") != "rejected":
            logger.info({"message": "repository granted", "assignment_id": row["id"]})
            reply["repo"] = repo
            reply["granted_repos"] = granted
        return reply

    async def provide_file(
        self, row: dict[str, Any], raw_name: Any, content: Any
    ) -> dict[str, Any]:
        """Write ``content`` to ``inbox/<name>`` of the parked ``row`` and resume it.

        The manager writes the file, so the session never gets a path out of
        ``inbox/``. The caps of ``inbox`` hold, and the row records the file."""
        if not self.section.enabled:
            return rejected("disabled", "assignments.enabled is false in joshua.yaml")
        refused = self._not_waiting(row)
        if refused is not None:
            return refused
        if not isinstance(content, str):
            return rejected("invalid_arguments", "content is text")
        data = content.encode("utf-8")
        files = inbox.records(row.get("inbox"))
        try:
            name = inbox.check_name(raw_name)
            inbox.check_caps(files, name, len(data))
        except inbox.InboxRefused as exc:
            return rejected(exc.reason, exc.message)
        work = self._work_dir(row["id"])
        if work is None:
            return rejected("no_scratch", "this runner has no scratch directory")

        async def stored() -> dict[str, Any]:
            current = await self.store.get(row["id"]) or row
            now_files = inbox.records(current.get("inbox"))
            inbox.check_caps(now_files, name, len(data))
            await asyncio.to_thread(inbox.write, work, name, data)
            return {"inbox": inbox.with_record(now_files, name, len(data), self.clock())}

        try:
            reply = await self._resume(
                row, prompts.provided_text(name, len(data)), what="file", stored=stored
            )
        except inbox.InboxRefused as exc:
            return rejected(exc.reason, exc.message)
        if reply.get("status") != "rejected":
            logger.info(
                {"message": "file provided", "assignment_id": row["id"], "bytes": len(data)}
            )
            reply["file"] = {"name": name, "path": f"{inbox.INBOX_DIR}/{name}", "bytes": len(data)}
        return reply

    def _work_dir(self, assignment_id: int) -> Path | None:
        work_dir = getattr(self.runner, "work_dir", None)
        return work_dir(int(assignment_id)) if work_dir is not None else None

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
        if row["status"] == DRAFT and row.get("started_at") is None:
            return rejected(
                "draft",
                f"assignment {row['id']} is a draft and never ran; delete it with "
                f"delete_assignment({row['id']})",
            )
        # The slot lock holds a start until the session stopped and the row is
        # ``cancelled``, so a start right after this call finds the slot free.
        async with self._slot_lock:
            current = await self.store.get(row["id"]) or row
            if current["status"] in FINISHED:
                return rejected(
                    "finished", f"assignment {row['id']} has ended: {current['status']}"
                )
            await self.runner.cancel(current)
            now = self.clock()
            await self.store.update(row["id"], status="cancelled", finished_at=now)
        await report.finalize(self.store, row["id"], now, self.load_config().timezone)
        logger.info({"message": "assignment cancelled", "assignment_id": row["id"]})
        return {
            "id": row["id"],
            "status": "cancelled",
            "message": f"Assignment {row['id']} is cancelled, and its slot is free. The plan "
            f"and the report stay; read them with assignment_status({row['id']}).",
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
            async with self._slot_lock:
                busy = await self._busy(
                    row["person_id"], f"extend assignment {row['id']} again when it ends"
                )
                if busy is not None:
                    busy["message"] = (
                        f"assignment {row['id']} keeps its caps and stays {row['status']}: "
                        f"{busy['message']}"
                    )
                    return busy
                current = await self._run(row, "extend", spec=spec, finished_at=None, error=None)
        else:
            current = await self.store.update(row["id"], spec=spec) or row
        logger.info(
            {"message": "assignment extended", "assignment_id": row["id"], "resume": resume}
        )
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
