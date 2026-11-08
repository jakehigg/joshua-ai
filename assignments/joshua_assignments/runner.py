"""The runner: the part that does the work of an assignment.

The manager sets an assignment ``running`` and calls ``start`` (a first
run) or ``resume`` (an assignment that ran before: after an answer, or
after a restart). ``cancel`` stops the work. Each call returns at once; the
work runs in the background. When the work stops, the runner writes the row
and calls ``RunnerHost.stopped``, and the manager sends the event.

``StubRunner`` makes no model call. It ends each assignment as ``done``
with a fixed summary, so the manager and the store are testable with no SDK.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from joshua_shared.log import get_logger

from joshua_assignments.store import Store

logger = get_logger("joshua_assignments.runner")

STUB_SUMMARY = (
    "The stub runner ran this assignment. No model call ran, and nothing changed. "
    "The plan page was not written."
)


class RunnerHost(Protocol):
    """What a runner needs from the manager."""

    store: Store
    clock: Callable[[], datetime]

    async def stopped(self, assignment_id: int) -> None: ...


class Runner(Protocol):
    async def start(self, assignment: dict[str, Any]) -> None: ...

    async def resume(self, assignment: dict[str, Any], text: str) -> None: ...

    async def cancel(self, assignment: dict[str, Any]) -> None: ...


class StubRunner:
    """A runner with no model. ``delay_s`` is how long a run takes; None never ends."""

    def __init__(self, host: RunnerHost, delay_s: float | None = 0.0) -> None:
        self.host = host
        self.delay_s = delay_s
        self.tasks: dict[int, asyncio.Task[None]] = {}

    async def start(self, assignment: dict[str, Any]) -> None:
        self._spawn(assignment["id"])

    async def resume(self, assignment: dict[str, Any], text: str) -> None:
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
        await store.update(
            assignment_id,
            status="done",
            summary=STUB_SUMMARY,
            follow_ups=[],
            finished_at=self.host.clock(),
        )
        logger.info({"message": "stub run finished", "assignment_id": assignment_id})
        await self.host.stopped(assignment_id)
