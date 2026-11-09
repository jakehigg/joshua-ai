"""The runner: the part that does the work of an assignment.

The manager sets an assignment ``running`` and calls ``start`` (a first
run) or ``resume`` (an assignment that ran before). ``resume`` gets the
answer, if any, and the reason: ``answer`` (after ``answer_assignment``),
``restart`` (the container started again while it ran), or ``extend``
(``extend_assignment`` raised a cap that it had reached). ``cancel`` stops
the work. Each call returns at once; the
work runs in the background. When the work stops, the runner writes the row
and calls ``RunnerHost.stopped``, and the manager sends the event.

``StubRunner`` makes no model call. It ends each assignment as ``done``
with a fixed summary and a fixed report, so the manager and the store are testable with no SDK.
``joshua_assignments.sdk_runner.SdkRunner`` is the runner with a real
session.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_assignments import report
from joshua_assignments.settings import Settings
from joshua_assignments.spec import assignment_title
from joshua_assignments.store import Store

logger = get_logger("joshua_assignments.runner")

STUB_SUMMARY = "The stub runner ran this assignment. No model call ran, and nothing changed."


def stub_report(row: dict[str, Any]) -> str:
    """The report of the stub runner, in the shape of the system prompt, with no cost."""
    spec = row.get("spec") or {}
    body = {
        "Highlights": "- **No model call** The stub runner changed nothing.",
        "What was asked": str(spec.get("goal") or "").strip() or "No goal.",
        "What was done": STUB_SUMMARY,
        "Outputs": "None.",
        "Decisions for you": "None.",
        "Follow-ups": "None.",
    }
    parts = [f"# {assignment_title(spec)}", "This is a test report of the stub runner."]
    parts += [f"## {name}\n\n{body[name]}" for name in report.SECTIONS]
    return "\n\n".join(parts)


class RunnerHost(Protocol):
    """What a runner needs from the manager."""

    store: Store
    settings: Settings
    clock: Callable[[], datetime]
    load_config: Callable[[], JoshuaConfig]

    async def stopped(self, assignment_id: int) -> None: ...


RESUME_REASONS = ("answer", "restart", "extend")


class Runner(Protocol):
    async def start(self, assignment: dict[str, Any]) -> None: ...

    async def resume(
        self, assignment: dict[str, Any], text: str, *, reason: str = "answer"
    ) -> None: ...

    async def cancel(self, assignment: dict[str, Any]) -> None: ...

    def work_dir(self, assignment_id: int) -> Path | None:
        """The scratch directory of an assignment, or None when the runner has none."""
        ...


class StubRunner:
    """A runner with no model. ``delay_s`` is how long a run takes; None never ends.

    ``work_root`` is where the scratch directories are, for ``provide_file``.
    With None, the stub has no scratch directory."""

    def __init__(
        self, host: RunnerHost, delay_s: float | None = 0.0, work_root: Path | None = None
    ) -> None:
        self.host = host
        self.delay_s = delay_s
        self.work_root = work_root
        self.tasks: dict[int, asyncio.Task[None]] = {}

    def work_dir(self, assignment_id: int) -> Path | None:
        if self.work_root is None:
            return None
        return self.work_root / str(int(assignment_id))

    async def start(self, assignment: dict[str, Any]) -> None:
        self._spawn(assignment["id"])

    async def resume(
        self, assignment: dict[str, Any], text: str, *, reason: str = "answer"
    ) -> None:
        self._spawn(assignment["id"])

    async def cancel(self, assignment: dict[str, Any]) -> None:
        task = self.tasks.pop(assignment["id"], None)
        if task is not None and not task.done():
            task.cancel()

    async def idle(self) -> None:
        """Wait until every run that can end has ended. For tests."""
        while running := [t for t in self.tasks.values() if not t.done()]:
            if self.delay_s is None:
                return
            await asyncio.gather(*running, return_exceptions=True)

    def _spawn(self, assignment_id: int) -> None:
        task = asyncio.create_task(self._run(assignment_id))
        self.tasks[assignment_id] = task
        task.add_done_callback(lambda done: self._forget(assignment_id, done))

    def _forget(self, assignment_id: int, task: asyncio.Task[None]) -> None:
        if self.tasks.get(assignment_id) is task:
            del self.tasks[assignment_id]

    async def _run(self, assignment_id: int) -> None:
        if self.delay_s is None:
            await asyncio.Event().wait()
        await asyncio.sleep(self.delay_s or 0)
        store = self.host.store
        await store.add_checkpoint(assignment_id, "the stub runner finished")
        row = await store.get(assignment_id)
        await store.write_report(assignment_id, stub_report(row or {}))
        now = self.host.clock()
        await store.update(
            assignment_id,
            status="done",
            summary=STUB_SUMMARY,
            follow_ups=[],
            finished_at=now,
        )
        await report.finalize(store, assignment_id, now, self.host.load_config().timezone)
        logger.info({"message": "stub run finished", "assignment_id": assignment_id})
        await self.host.stopped(assignment_id)
