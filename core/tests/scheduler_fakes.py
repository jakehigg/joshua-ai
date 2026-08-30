"""In-memory fakes for the offline scheduler and scheduling-tool tests.

``FakeTaskRepo`` mirrors the task, conversation, and channel methods the
scheduler and tools call, with the same status semantics as the real repo (claim
marks rows ``processing``; re-arm only touches a ``processing`` row). ``FakeManager``
and ``FakeDeliverer`` record what the scheduler does with a fired task.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from joshua_core.engine.types import TurnResult
from joshua_core.store.models import Channel, Conversation, Task


class FakeTaskRepo:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._conversations: dict[str, Conversation] = {}
        self._channels: dict[str, Channel] = {}
        self._next_id = 1

    # --- test setup --------------------------------------------------------

    def add_conversation(self, conv: Conversation) -> None:
        self._conversations[conv.id] = conv

    def add_channel(self, channel: Channel) -> None:
        self._channels[channel.id] = channel

    # --- conversations / channels -----------------------------------------

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self._conversations.get(conversation_id)

    async def get_channel(self, channel_id: str) -> Channel | None:
        return self._channels.get(channel_id)

    # --- tasks -------------------------------------------------------------

    async def create_task(
        self,
        conversation_id: str,
        prompt: str,
        next_run_at: datetime,
        cron_expr: str | None = None,
    ) -> Task:
        task_id = str(self._next_id)
        self._next_id += 1
        task = Task(
            id=task_id,
            conversation_id=conversation_id,
            prompt=prompt,
            next_run_at=next_run_at,
            cron_expr=cron_expr,
            status="active",
        )
        self._tasks[task_id] = task
        return task

    async def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def list_tasks(
        self, conversation_id: str | None = None, include_inactive: bool = False
    ) -> list[Task]:
        rows = [
            t
            for t in self._tasks.values()
            if (conversation_id is None or t.conversation_id == conversation_id)
            and (include_inactive or t.status in ("active", "paused"))
        ]
        return sorted(rows, key=lambda t: t.next_run_at)

    async def claim_due_tasks(self, now: datetime, limit: int = 25) -> list[Task]:
        due = sorted(
            (t for t in self._tasks.values() if t.status == "active" and t.next_run_at <= now),
            key=lambda t: t.next_run_at,
        )[:limit]
        claimed = []
        for task in due:
            self._tasks[task.id] = replace(task, status="processing")
            claimed.append(self._tasks[task.id])
        return claimed

    async def reschedule_task(
        self, task_id: str, next_run_at: datetime, error: str | None = None
    ) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.status != "processing":
            return False
        self._tasks[task_id] = replace(
            task, status="active", next_run_at=next_run_at, last_error=error
        )
        return True

    async def reset_processing(self) -> int:
        count = 0
        for task_id, task in list(self._tasks.items()):
            if task.status == "processing":
                self._tasks[task_id] = replace(task, status="active")
                count += 1
        return count

    async def complete_task(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if task is not None:
            self._tasks[task_id] = replace(task, status="completed")

    async def fail_task(self, task_id: str, error: str, reactivate: bool) -> None:
        task = self._tasks.get(task_id)
        if task is not None:
            self._tasks[task_id] = replace(
                task, status="active" if reactivate else "failed", last_error=error
            )

    async def set_task_status(
        self, task_id: str, status: str, conversation_id: str | None = None
    ) -> bool:
        task = self._tasks.get(task_id)
        if task is None or (conversation_id and task.conversation_id != conversation_id):
            return False
        self._tasks[task_id] = replace(task, status=status)
        return True

    async def update_task(
        self,
        task_id: str,
        prompt: str | None = None,
        cron_expr: str | None = None,
        next_run_at: datetime | None = None,
        conversation_id: str | None = None,
    ) -> bool:
        task = self._tasks.get(task_id)
        if task is None or (conversation_id and task.conversation_id != conversation_id):
            return False
        changes: dict[str, Any] = {}
        if prompt is not None:
            changes["prompt"] = prompt
        if cron_expr is not None:
            changes["cron_expr"] = cron_expr or None
        if next_run_at is not None:
            changes["next_run_at"] = next_run_at
        if not changes:
            return False
        self._tasks[task_id] = replace(task, **changes)
        return True


class FakeManager:
    """Records each fired turn. ``on_turn`` may mutate state (e.g. cancel the
    task) to model the agent acting during the run. ``reply`` is the turn text."""

    def __init__(self, reply: str = "reminder!", on_turn: Any = None) -> None:
        self.reply = reply
        self._on_turn = on_turn
        self.calls: list[dict[str, Any]] = []

    async def run_turn(
        self,
        channel: Channel,
        conversation: Conversation,
        text: str,
        *,
        framing: str | None = None,
        direction: str = "in",
        **kwargs: Any,
    ) -> TurnResult:
        self.calls.append(
            {
                "channel_id": channel.id,
                "conversation_id": conversation.id,
                "text": text,
                "framing": framing,
                "direction": direction,
            }
        )
        if self._on_turn is not None:
            await self._on_turn()
        return TurnResult(text=self.reply, session_id="stub")


class FakeDeliverer:
    def __init__(self) -> None:
        self.delivered: list[tuple[str, str]] = []

    async def deliver(self, target: str, text: str, attachments: Any = ()) -> bool:
        self.delivered.append((target, text))
        return True
