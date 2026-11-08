"""The SDK runner: one Claude Agent SDK session for each assignment, run in slices.

``SdkRunner`` implements ``Runner``. The manager sets an assignment
``running`` and calls ``start`` or ``resume``. The runner then works in
slices of ``assignments.slice_turns`` model turns:

1. Read core's ``GET /v1/limits``. At or over a ceiling, stop as
   ``paused_limits``. With unknown limits, ``when_unknown`` decides.
2. Run one slice. A ``RateLimitEvent`` at or over a ceiling interrupts it.
3. Add the usage, the turns, and the working time to the row, store the
   slice, and send its usage to core's ``POST /v1/usage``.
4. End on ``finish`` (``done``), the token cap or the turns
   (``budget_exceeded``), the working time (``time_exceeded``), or a session
   error (``failed``). ``ask`` parks the assignment as ``waiting``.

The session id is on the row, and each slice passes it as ``resume``, so a
stop, an answer, or a restart continues the same session. The scratch
directory ``/work/<id>/`` holds the session (``HOME`` and
``CLAUDE_CONFIG_DIR`` are in it). It stays while the assignment is parked or
failed, and goes when it ends in another way. ``sweep`` removes the scratch
of a failed assignment after ``assignments.keep_failed_work``.

A failure gives the row a short label, never the error text.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

from joshua_shared.config import parse_duration
from joshua_shared.contracts import USAGE_MAX_COUNT, UsageWriteCounts, UsageWriteRow
from joshua_shared.gateway_mcp import allowed_gateway_servers, gateway_servers
from joshua_shared.limits import parse_event_windows
from joshua_shared.log import get_logger

from joshua_assignments import budget, prompts, session
from joshua_assignments.clock import Clock
from joshua_assignments.core_client import CoreClient
from joshua_assignments.runner import RunnerHost
from joshua_assignments.store import FINISHED, USAGE_FIELDS

logger = get_logger("joshua_assignments.sdk_runner")

WORK_ROOT = Path("/work")
MAX_WAIT_S = 600
MAX_NOTE = 2000
MAX_QUESTION = 2000
MAX_SUMMARY = 4000
MAX_FOLLOW_UPS = 20
MAX_FOLLOW_UP = 500
CANCEL_TIMEOUT_S = 30.0
# The server that the plan page needs. ``tools`` in a spec never removes it.
FILES_SERVER = "files"
_LABEL_RE = re.compile(r"^[A-Za-z0-9\[][A-Za-z0-9._:/@\[\]-]{0,63}\Z")

ASK_REPLY = (
    "The assignment pauses here; the answer arrives in your next turn. "
    "Stop now and make no other tool call."
)
FINISH_REPLY = "The assignment is finished. Stop now."


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _reply(text: str, is_error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["is_error"] = True
    return out


def usage_ref(assignment_id: int, n: int) -> str:
    """The ``ref`` of the usage row of one slice. Core stores it as ``assignments:<ref>``."""
    return f"{assignment_id}:slice:{n}"


def total_tokens(row: Mapping[str, Any]) -> int:
    """The tokens of an assignment: input, output, cache read, and cache write, added."""
    return sum(int(row.get(name) or 0) for name in USAGE_FIELDS)


class AssignmentTools:
    """The handlers of the in-process ``assignment`` server, for one run."""

    def __init__(
        self,
        assignment_id: int,
        host: RunnerHost,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.assignment_id = assignment_id
        self.host = host
        self.sleep = sleep
        self.finished: dict[str, Any] | None = None
        self.question: str | None = None

    @property
    def stop_requested(self) -> bool:
        return self.finished is not None or self.question is not None

    def handlers(self) -> dict[str, session.ToolHandler]:
        return {
            "checkpoint": self.checkpoint,
            "ask": self.ask,
            "wait": self.wait,
            "finish": self.finish,
        }

    async def checkpoint(self, args: dict[str, Any]) -> dict[str, Any]:
        note = _text(args.get("note"))
        if not note:
            return _reply("Give a note.", is_error=True)
        await self.host.store.add_checkpoint(self.assignment_id, note[:MAX_NOTE])
        return _reply("Checkpoint stored.")

    async def ask(self, args: dict[str, Any]) -> dict[str, Any]:
        question = _text(args.get("question"))
        if not question:
            return _reply("Give one clear question.", is_error=True)
        self.question = question[:MAX_QUESTION]
        await self.host.store.update(
            self.assignment_id, open_question=self.question, asked_at=self.host.clock()
        )
        logger.info({"message": "ask", "assignment_id": self.assignment_id})
        return _reply(ASK_REPLY)

    async def wait(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            seconds = int(args.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0
        seconds = max(1, min(seconds, MAX_WAIT_S))
        await self.sleep(seconds)
        return _reply(f"Waited {seconds} seconds.")

    async def finish(self, args: dict[str, Any]) -> dict[str, Any]:
        summary = _text(args.get("summary"))
        if not summary:
            return _reply("Give the summary.", is_error=True)
        raw = args.get("follow_ups") or []
        items = raw if isinstance(raw, list) else [raw]
        follow_ups = [_text(item)[:MAX_FOLLOW_UP] for item in items if _text(item)]
        self.finished = {
            "summary": summary[:MAX_SUMMARY],
            "follow_ups": follow_ups[:MAX_FOLLOW_UPS],
        }
        logger.info({"message": "finish", "assignment_id": self.assignment_id})
        return _reply(FINISH_REPLY)


class SdkRunner:
    """The runner with a real session. See the module docstring."""

    def __init__(
        self,
        host: RunnerHost,
        *,
        core: CoreClient | None = None,
        client_factory: Callable[..., Any] | None = None,
        work_root: Path = WORK_ROOT,
        environ: Mapping[str, str] | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        watch_s: float = session.WATCH_S,
    ) -> None:
        self.host = host
        self.core = core or CoreClient(host.settings)
        self.client_factory = client_factory
        self.work_root = work_root
        self.environ = os.environ if environ is None else environ
        self.sleep = sleep
        self.monotonic = monotonic
        self.watch_s = watch_s
        self.tasks: dict[int, asyncio.Task[None]] = {}

    # --- the Runner protocol ----------------------------------------------

    async def start(self, assignment: dict[str, Any]) -> None:
        self._spawn(assignment["id"], after_stop=False, answer=None)

    async def resume(self, assignment: dict[str, Any], text: str) -> None:
        self._spawn(assignment["id"], after_stop=True, answer=text or None)

    async def cancel(self, assignment: dict[str, Any]) -> None:
        """Stop the session of the assignment and remove its scratch directory."""
        assignment_id = assignment["id"]
        task = self.tasks.pop(assignment_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(task, CANCEL_TIMEOUT_S)
        self.remove_work(assignment_id)

    async def idle(self) -> None:
        """Wait until every run has ended. For tests."""
        while running := [t for t in self.tasks.values() if not t.done()]:
            await asyncio.gather(*running, return_exceptions=True)

    # --- the scratch directory --------------------------------------------

    def work_dir(self, assignment_id: int) -> Path:
        return self.work_root / str(int(assignment_id))

    def remove_work(self, assignment_id: int) -> None:
        shutil.rmtree(self.work_dir(assignment_id), ignore_errors=True)

    async def sweep(self) -> list[int]:
        """Remove the scratch of each ended assignment, a failed one after ``keep_failed_work``.

        Returns the ids whose scratch it removed.
        """
        if not self.work_root.is_dir():
            return []
        keep = timedelta(
            seconds=parse_duration(self.host.load_config().assignments.keep_failed_work)
        )
        now = self.host.clock()
        removed: list[int] = []
        for entry in sorted(self.work_root.iterdir()):
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            assignment_id = int(entry.name)
            if assignment_id in self.tasks:
                continue
            row = await self.host.store.get(assignment_id)
            if row is not None and row["status"] not in FINISHED:
                continue
            if row is not None and row["status"] == "failed":
                finished_at = row.get("finished_at")
                if finished_at is not None and now - finished_at < keep:
                    continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(assignment_id)
        if removed:
            logger.info({"message": "scratch removed", "assignment_ids": removed})
        return removed

    # --- one run ----------------------------------------------------------

    def _spawn(self, assignment_id: int, *, after_stop: bool, answer: str | None) -> None:
        task = asyncio.create_task(self._run(assignment_id, after_stop, answer))
        self.tasks[assignment_id] = task
        task.add_done_callback(lambda done: self._forget(assignment_id, done))

    def _forget(self, assignment_id: int, task: asyncio.Task[None]) -> None:
        if self.tasks.get(assignment_id) is task:
            del self.tasks[assignment_id]

    async def _run(self, assignment_id: int, after_stop: bool, answer: str | None) -> None:
        try:
            await self._work(assignment_id, after_stop, answer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — every failure ends the assignment
            logger.error(
                {
                    "message": "the run failed",
                    "assignment_id": assignment_id,
                    "error": type(exc).__name__,
                }
            )
            await self._end(assignment_id, "failed", error=f"runner_{type(exc).__name__}")

    async def _end(self, assignment_id: int, status: str, **fields: Any) -> None:
        """End or park the assignment, keep or remove its scratch, and tell the host."""
        if status in FINISHED:
            fields["finished_at"] = self.host.clock()
        await self.host.store.update(assignment_id, status=status, **fields)
        if status in FINISHED and status != "failed":
            self.remove_work(assignment_id)
        logger.info(
            {"message": "assignment stopped", "assignment_id": assignment_id, "status": status}
        )
        await self.host.stopped(assignment_id)

    async def _park_limits(
        self, row: dict[str, Any], check: budget.LimitsCheck, ceilings: budget.Ceilings, source: str
    ) -> None:
        checkpoint = await self.host.store.last_checkpoint(row["id"])
        record = budget.stop_record(
            check,
            ceilings,
            at=self.host.clock(),
            source=source,
            next_step_text=budget.next_step(checkpoint["note"] if checkpoint else None),
        )
        await self._end(row["id"], "paused_limits", stopped_at_limits=record)

    def _gateway(self, row: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """The gateway MCP servers of the person, built as core builds them."""
        cfg = self.host.load_config()
        person = row["person_id"]
        names = [
            name for name in allowed_gateway_servers(cfg, person) if name != session.MANAGER_SERVER
        ]
        narrow = (row.get("spec") or {}).get("tools")
        if narrow is not None:
            names = [name for name in names if name in narrow or name == FILES_SERVER]
        settings = self.host.settings
        assert settings.gateway_url is not None
        return gateway_servers(
            names,
            settings.gateway_url,
            settings.fleet_token,
            person,
            f"assignment:{row['id']}",
            role="member",
        )

    async def _work(self, assignment_id: int, after_stop: bool, answer: str | None) -> None:
        store = self.host.store
        row = await store.get(assignment_id)
        if row is None or row["status"] != "running":
            return
        settings = self.host.settings
        if not settings.can_run:
            await self._end(assignment_id, "failed", error="not_configured")
            return
        work = self.work_dir(assignment_id)
        home = work / ".home"
        config_dir = home / ".claude"
        config_dir.mkdir(parents=True, exist_ok=True)
        tools = AssignmentTools(assignment_id, self.host, self.sleep)
        server = session.assignment_server(tools.handlers())
        env = session.session_env(
            self.environ,
            core_url=settings.core_url or "",
            token=settings.fleet_token,
            home=home,
            config_dir=config_dir,
        )

        while True:
            row = await store.get(assignment_id)
            if row is None or row["status"] != "running":
                return
            section = self.host.load_config().assignments
            spec = row.get("spec") or {}
            ceilings = budget.Ceilings.from_spec(spec)
            tokens_cap = (spec.get("budget") or {}).get("tokens")
            max_turns = int(spec.get("max_turns") or section.default_max_turns)
            working_s = parse_duration(
                str(spec.get("working_time") or section.default_working_time)
            )
            turns = int(row.get("turns") or 0)
            used_s = int(row.get("working_s") or 0)

            if tokens_cap and total_tokens(row) >= int(tokens_cap):
                await self._end(assignment_id, "budget_exceeded", error="tokens")
                return
            if turns >= max_turns:
                await self._end(assignment_id, "budget_exceeded", error="max_turns")
                return
            if used_s >= working_s:
                await self._end(assignment_id, "time_exceeded", error="working_time")
                return

            check = budget.check_limits(await self.core.limits(), ceilings)
            if check.over is not None or budget.blocked_when_unknown(
                check, section.limits.when_unknown, tokens_cap
            ):
                await self._park_limits(row, check, ceilings, source="limits")
                return

            checkpoint = await store.last_checkpoint(assignment_id)
            if row.get("session_id"):
                prompt = prompts.later_prompt(
                    budget=budget.budget_line(
                        check,
                        ceilings,
                        turns_left=max_turns - turns,
                        working_left_s=working_s - used_s,
                        tokens_left=int(tokens_cap) - total_tokens(row) if tokens_cap else None,
                    ),
                    checkpoint=checkpoint["note"] if checkpoint else None,
                    after_stop=after_stop,
                    answer=answer,
                )
            else:
                prompt = prompts.first_prompt(row)
            after_stop, answer = False, None

            persona = section.models.get(str(spec.get("model") or ""))
            options = session.build_options(
                system_prompt=prompts.system_prompt(),
                cwd=work,
                model=row["model"],
                effort=persona.effort if persona else None,
                max_turns=max(1, min(section.slice_turns, max_turns - turns)),
                env=env,
                gateway=self._gateway(row),
                gateway_url=settings.gateway_url or "",
                assignment=server,
                resume=row.get("session_id"),
            )
            n = await store.next_slice_n(assignment_id)
            clock = Clock(working_s - used_s, now=self.monotonic)
            started_at = self.host.clock()
            outcome = session.SliceOutcome()

            def over_ceiling(info: Any, ceilings: budget.Ceilings = ceilings) -> bool:
                return budget.check_windows(parse_event_windows(info), ceilings).over is not None

            async def keep_session_id(session_id: str) -> None:
                await store.update(assignment_id, session_id=session_id)

            reason = "cancelled"
            try:
                await session.run_slice(
                    options,
                    prompt,
                    clock=clock,
                    over_ceiling=over_ceiling,
                    stop_requested=lambda: tools.stop_requested,
                    on_session_id=keep_session_id,
                    watch_s=self.watch_s,
                    outcome=outcome,
                    **({"client_factory": self.client_factory} if self.client_factory else {}),
                )
                reason = self._reason(tools, outcome)
            finally:
                await self._record(row, n, outcome, clock, check, started_at, reason)

            logger.info(
                {
                    "message": "slice ended",
                    "assignment_id": assignment_id,
                    "slice": n,
                    "reason": reason,
                    "turns": outcome.turns,
                }
            )
            if tools.finished is not None:
                await self._end(assignment_id, "done", **tools.finished)
                return
            if tools.question is not None:
                await self._end(assignment_id, "waiting")
                return
            if outcome.stop == "limits":
                event = budget.check_windows(parse_event_windows(outcome.rate_limit_info), ceilings)
                # Keep the windows of the read before the slice that the event does not give.
                merged = budget.LimitsCheck(
                    windows={**check.windows, **event.windows},
                    unknown=tuple(w for w in event.unknown if w not in check.windows),
                    over=event.over,
                )
                fresh = await store.get(assignment_id) or row
                await self._park_limits(fresh, merged, ceilings, source="event")
                return
            if outcome.error is not None:
                await self._end(assignment_id, "failed", error=outcome.error)
                return
            if outcome.stop == "time":
                await self._end(assignment_id, "time_exceeded", error="working_time")
                return

    @staticmethod
    def _reason(tools: AssignmentTools, outcome: session.SliceOutcome) -> str:
        if tools.finished is not None:
            return "finish"
        if tools.question is not None:
            return "ask"
        if outcome.stop is not None:
            return outcome.stop
        if outcome.error is not None:
            return "error"
        if outcome.subtype == session.MAX_TURNS_SUBTYPE:
            return "turns"
        return "end"

    async def _record(
        self,
        row: dict[str, Any],
        n: int,
        outcome: session.SliceOutcome,
        clock: Clock,
        check: budget.LimitsCheck,
        started_at: Any,
        reason: str,
    ) -> None:
        """Add the slice to the row, store it, and send its usage to core."""
        assignment_id = row["id"]
        usage = outcome.usage or dict.fromkeys(USAGE_FIELDS, 0)
        current = await self.host.store.get(assignment_id) or row
        await self.host.store.update(
            assignment_id,
            turns=int(current.get("turns") or 0) + outcome.turns,
            working_s=int(current.get("working_s") or 0) + round(max(0.0, clock.elapsed())),
            **{name: int(current.get(name) or 0) + usage[name] for name in USAGE_FIELDS},
        )
        await self.host.store.add_slice(
            assignment_id,
            n=n,
            usage=usage,
            limits_before=check.windows or None,
            started_at=started_at,
            ended_at=self.host.clock(),
            reason=reason,
        )
        if not any(usage.values()):
            return
        worker = str((row.get("spec") or {}).get("model") or "")
        model = str(row.get("model") or "")
        await self.core.post_usage(
            UsageWriteRow(
                kind="assignment",
                name="assignment",
                worker=worker if _LABEL_RE.match(worker) else None,
                run_id=assignment_id,
                person_id=row["person_id"],
                model=model if _LABEL_RE.match(model) else None,
                calls=max(1, outcome.calls),
                usage=UsageWriteCounts(
                    **{name: min(usage[name], USAGE_MAX_COUNT) for name in USAGE_FIELDS}
                ),
                ref=usage_ref(assignment_id, n),
            )
        )
