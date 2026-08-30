"""The Scheduler: fire due tasks back into their conversation, through channels.

One background loop per core process. Each tick claims the due tasks (atomically,
so a slow turn never double-fires), runs each as a turn framed as a scheduled
firing, delivers any reply to the conversation's channel, then re-arms a cron
task or completes a one-time task. Orphaned ``processing`` rows from a crash are
re-queued on boot.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from joshua_shared.log import get_logger

from joshua_core.delivery import Deliverer, is_ignore
from joshua_core.engine.cron import compute_next_cron, now_in
from joshua_core.store.models import Task
from joshua_core.store.repo import Repo

logger = get_logger("scheduler")

# Fallback framing when the packaged prompt file is missing.
_SCHEDULED_FALLBACK = (
    "This is a scheduled task firing now — not a live message from a person. "
    "Carry out the instruction below. If it should result in a message to the "
    "person, phrase that message the way it should reach them on this channel. "
    "If nothing needs to be said, reply with exactly `[IGNORE]`."
)

# Appended for a recurring task so the agent can stop it once it is no longer
# needed. The recurring-task self-cancel framing.
_RECURRING_NOTE = (
    "This is a recurring task (id {task_id}). It keeps running on its schedule. "
    "If it is done or no longer wanted, call the `scheduling` tool `cancel_task` "
    "with this id to stop it."
)


class Scheduler:
    """Poll for due tasks and fire them as scheduled turns."""

    def __init__(
        self,
        *,
        repo: Repo,
        manager: object,
        deliverer: Deliverer,
        tz: str,
        tick_seconds: int = 15,
        batch_limit: int = 25,
        prompts_dir: Path | str | None = None,
    ) -> None:
        self._repo = repo
        self._manager = manager
        self._deliverer = deliverer
        self._tz = tz
        self._tick = max(1, tick_seconds)
        self._batch_limit = batch_limit
        base = Path(prompts_dir) if prompts_dir else Path(__file__).parent / "prompts"
        self._scheduled_file = base / "builtin" / "scheduled.md"
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Re-queue orphaned rows, then run the loop in the background."""
        requeued = await self._repo.reset_processing()
        if requeued:
            logger.info({"message": "re-queued orphaned tasks on boot", "count": requeued})
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="scheduler-loop")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self._sweep()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — a bad tick must not kill the loop
                logger.error({"message": "scheduler sweep error", "error": str(exc)})
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._tick)
            except TimeoutError:
                pass

    # --- firing ------------------------------------------------------------

    async def _sweep(self) -> None:
        now = now_in(self._tz)
        due = await self._repo.claim_due_tasks(now, limit=self._batch_limit)
        for task in due:
            await self._fire(task)

    def _framing_for(self, task: Task) -> str:
        base = self._scheduled_prompt()
        if task.cron_expr:
            return base + "\n\n" + _RECURRING_NOTE.format(task_id=task.id)
        return base

    def _scheduled_prompt(self) -> str:
        try:
            return self._scheduled_file.read_text().strip()
        except OSError:
            return _SCHEDULED_FALLBACK

    async def _fire(self, task: Task) -> None:
        conv = await self._repo.get_conversation(task.conversation_id)
        if conv is None:
            logger.warning({"message": "task has no conversation; dropping", "task_id": task.id})
            await self._repo.complete_task(task.id)
            return
        target = conv.last_channel_id or conv.channel_id
        channel = await self._repo.get_channel(target)
        if channel is None:
            logger.warning(
                {
                    "message": "task target channel missing; dropping",
                    "task_id": task.id,
                    "target": target,
                }
            )
            await self._repo.complete_task(task.id)
            return

        try:
            result = await self._manager.run_turn(
                channel, conv, task.prompt, framing=self._framing_for(task), direction="task"
            )
        except Exception as exc:  # noqa: BLE001 — a failed turn must not stall the sweep
            logger.error(
                {"message": "scheduled turn failed", "task_id": task.id, "error": str(exc)}
            )
            await self._repo.fail_task(task.id, str(exc), reactivate=bool(task.cron_expr))
            return

        text = result.text or ""
        if text and not is_ignore(text):
            await self._deliverer.deliver(target, text)

        await self._settle(task)

    async def _settle(self, task: Task) -> None:
        """Re-arm a cron task or complete a one-time task.

        A recurring task re-arms only if it is still ``processing`` — if the agent
        cancelled or paused it during the run, that change stays durable and
        ``reschedule_task`` returns False.
        """
        if not task.cron_expr:
            await self._repo.complete_task(task.id)
            return
        try:
            next_run = compute_next_cron(task.cron_expr, now_in(self._tz), self._tz)
        except ValueError as exc:
            # A stored cron is validated at write time, so this is unexpected; fail
            # the row instead of leaving it 'processing' and re-firing every boot.
            logger.error(
                {
                    "message": "invalid cron on task; failing it",
                    "task_id": task.id,
                    "error": str(exc),
                }
            )
            await self._repo.fail_task(task.id, f"invalid cron: {exc}", reactivate=False)
            return
        rescheduled = await self._repo.reschedule_task(task.id, next_run)
        if not rescheduled:
            logger.info(
                {"message": "recurring task not re-armed (changed during run)", "task_id": task.id}
            )
