"""Unit tests for the Scheduler loop (offline: fake repo, manager, deliverer)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from joshua_core.engine.cron import now_in
from joshua_core.scheduler import Scheduler
from joshua_core.store.models import Channel, Conversation
from scheduler_fakes import FakeDeliverer, FakeManager, FakeTaskRepo

TZ = "America/New_York"


def _wire(repo: FakeTaskRepo, *, last_channel_id: str | None = None) -> None:
    repo.add_channel(Channel(id="telegram:1", channel_type="telegram"))
    if last_channel_id:
        repo.add_channel(Channel(id=last_channel_id, channel_type="telegram"))
    repo.add_conversation(
        Conversation(
            id="c1", channel_id="telegram:1", person_id="alex", last_channel_id=last_channel_id
        )
    )


def _scheduler(repo, manager, deliverer, **kw) -> Scheduler:
    return Scheduler(repo=repo, manager=manager, deliverer=deliverer, tz=TZ, **kw)


async def test_one_shot_fires_delivers_and_completes() -> None:
    repo = FakeTaskRepo()
    _wire(repo)
    manager, deliverer = FakeManager(reply="stand up now"), FakeDeliverer()
    task = await repo.create_task("c1", "remind me", now_in(TZ) - timedelta(seconds=1))

    await _scheduler(repo, manager, deliverer)._sweep()

    assert deliverer.delivered == [("telegram:1", "stand up now")]
    assert manager.calls[0]["direction"] == "task"
    assert (await repo.get_task(task.id)).status == "completed"


async def test_delivery_targets_last_channel() -> None:
    repo = FakeTaskRepo()
    _wire(repo, last_channel_id="telegram:9")
    manager, deliverer = FakeManager(), FakeDeliverer()
    await repo.create_task("c1", "hi", now_in(TZ) - timedelta(seconds=1))

    await _scheduler(repo, manager, deliverer)._sweep()

    assert deliverer.delivered[0][0] == "telegram:9"


async def test_ignore_reply_is_not_delivered() -> None:
    repo = FakeTaskRepo()
    _wire(repo)
    manager, deliverer = FakeManager(reply="[IGNORE]"), FakeDeliverer()
    task = await repo.create_task("c1", "quiet task", now_in(TZ) - timedelta(seconds=1))

    await _scheduler(repo, manager, deliverer)._sweep()

    assert deliverer.delivered == []
    assert (await repo.get_task(task.id)).status == "completed"


async def test_cron_task_rearms() -> None:
    repo = FakeTaskRepo()
    _wire(repo)
    manager, deliverer = FakeManager(), FakeDeliverer()
    task = await repo.create_task(
        "c1", "weekly", now_in(TZ) - timedelta(seconds=1), cron_expr="*/5 * * * *"
    )

    await _scheduler(repo, manager, deliverer)._sweep()

    reloaded = await repo.get_task(task.id)
    assert reloaded.status == "active"
    assert reloaded.next_run_at > now_in(TZ)


async def test_cron_cancelled_during_turn_stays_cancelled() -> None:
    repo = FakeTaskRepo()
    _wire(repo)
    deliverer = FakeDeliverer()
    task = await repo.create_task(
        "c1", "weekly", now_in(TZ) - timedelta(seconds=1), cron_expr="*/5 * * * *"
    )

    async def cancel_during_turn() -> None:
        # The agent cancels the task from inside the fired turn.
        await repo.set_task_status(task.id, "completed", "c1")

    manager = FakeManager(on_turn=cancel_during_turn)
    await _scheduler(repo, manager, deliverer)._sweep()

    # reschedule_task saw a non-processing row and returned False.
    assert (await repo.get_task(task.id)).status == "completed"


async def test_failed_turn_reactivates_cron_but_fails_one_shot() -> None:
    repo = FakeTaskRepo()
    _wire(repo)

    async def boom() -> None:
        raise RuntimeError("turn broke")

    manager = FakeManager(on_turn=boom)
    one = await repo.create_task("c1", "once", now_in(TZ) - timedelta(seconds=1))
    rec = await repo.create_task(
        "c1", "again", now_in(TZ) - timedelta(seconds=1), cron_expr="*/5 * * * *"
    )

    await _scheduler(repo, manager, FakeDeliverer())._sweep()

    assert (await repo.get_task(one.id)).status == "failed"
    assert (await repo.get_task(rec.id)).status == "active"


async def test_invalid_stored_cron_fails_task_no_reboot_loop() -> None:
    repo = FakeTaskRepo()
    _wire(repo)
    # A row whose cron is somehow invalid must not re-fire on every boot.
    task = await repo.create_task(
        "c1", "broken", now_in(TZ) - timedelta(seconds=1), cron_expr="not a cron"
    )

    await _scheduler(repo, FakeManager(), FakeDeliverer())._sweep()

    assert (await repo.get_task(task.id)).status == "failed"


async def test_missing_conversation_is_dropped() -> None:
    repo = FakeTaskRepo()  # no conversation wired
    task = await repo.create_task("gone", "x", now_in(TZ) - timedelta(seconds=1))
    await _scheduler(repo, FakeManager(), FakeDeliverer())._sweep()
    assert (await repo.get_task(task.id)).status == "completed"


def test_framing_adds_recurring_note() -> None:
    repo = FakeTaskRepo()
    sched = _scheduler(repo, FakeManager(), FakeDeliverer())

    one_shot = _task(repo, cron=None)
    recurring = _task(repo, cron="*/5 * * * *")
    one_framing = sched._framing_for(one_shot)
    rec_framing = sched._framing_for(recurring)

    assert "scheduled task" in one_framing.lower()
    assert "cancel_task" not in one_framing
    assert "cancel_task" in rec_framing
    assert recurring.id in rec_framing


def _task(repo, *, cron):
    from joshua_core.store.models import Task

    return Task(id="t9", conversation_id="c1", prompt="p", next_run_at=now_in(TZ), cron_expr=cron)


async def test_start_requeues_orphans_and_loop_fires() -> None:
    repo = FakeTaskRepo()
    _wire(repo)
    manager, deliverer = FakeManager(reply="up"), FakeDeliverer()
    # An orphaned 'processing' row from a previous crash, already due.
    task = await repo.create_task("c1", "recover me", now_in(TZ) - timedelta(seconds=1))
    await repo.set_task_status(task.id, "processing")

    sched = _scheduler(repo, manager, deliverer, tick_seconds=1)
    await sched.start()
    try:
        for _ in range(50):
            if deliverer.delivered:
                break
            await asyncio.sleep(0.02)
    finally:
        await sched.stop()

    assert deliverer.delivered == [("telegram:1", "up")]
    assert (await repo.get_task(task.id)).status == "completed"
