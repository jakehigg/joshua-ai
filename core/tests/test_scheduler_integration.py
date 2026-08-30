"""Scheduler against the real Postgres repo — the claim/reschedule/reset
semantics that the offline fakes only mirror. Uses fake manager and deliverer so
no SDK or network is involved; the store is real.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from joshua_core.engine.cron import now_in
from joshua_core.scheduler import Scheduler
from scheduler_fakes import FakeDeliverer, FakeManager

pytestmark = pytest.mark.integration

TZ = "America/New_York"


async def _seed_conversation(repo) -> str:
    await repo.upsert_channel("telegram:1", "telegram", session_mode="per_person")
    await repo.upsert_person("alex", "Alex", "member")
    conv = await repo.get_or_create_conversation("telegram:1", "alex")
    return conv.id


async def test_one_shot_fires_delivers_and_completes(repo) -> None:
    conv_id = await _seed_conversation(repo)
    task = await repo.create_task(conv_id, "remind me", now_in(TZ) - timedelta(seconds=1))
    manager, deliverer = FakeManager(reply="stand up"), FakeDeliverer()

    await Scheduler(repo=repo, manager=manager, deliverer=deliverer, tz=TZ)._sweep()

    assert deliverer.delivered == [("telegram:1", "stand up")]
    assert (await repo.get_task(task.id)).status == "completed"


async def test_cron_rearms_then_self_cancel_sticks(repo) -> None:
    conv_id = await _seed_conversation(repo)
    task = await repo.create_task(
        conv_id, "weekly", now_in(TZ) - timedelta(seconds=1), cron_expr="*/5 * * * *"
    )

    # First firing re-arms the recurring task.
    await Scheduler(repo=repo, manager=FakeManager(), deliverer=FakeDeliverer(), tz=TZ)._sweep()
    reloaded = await repo.get_task(task.id)
    assert reloaded.status == "active"
    assert reloaded.next_run_at > now_in(TZ)

    # Make it due again; this time the agent cancels it during the turn, so the
    # reschedule sees a non-'processing' row and does not re-arm it.
    await repo.update_task(task.id, next_run_at=now_in(TZ) - timedelta(seconds=1))

    async def cancel_during_turn() -> None:
        await repo.set_task_status(task.id, "completed", conv_id)

    manager = FakeManager(on_turn=cancel_during_turn)
    await Scheduler(repo=repo, manager=manager, deliverer=FakeDeliverer(), tz=TZ)._sweep()
    assert (await repo.get_task(task.id)).status == "completed"


async def test_start_requeues_orphaned_processing_rows(repo) -> None:
    conv_id = await _seed_conversation(repo)
    task = await repo.create_task(conv_id, "recover", now_in(TZ) - timedelta(seconds=1))
    # Simulate a crash mid-run: the row is left 'processing'.
    await repo.claim_due_tasks(now_in(TZ))
    assert (await repo.get_task(task.id)).status == "processing"

    sched = Scheduler(
        repo=repo, manager=FakeManager(reply="up"), deliverer=FakeDeliverer(), tz=TZ, tick_seconds=1
    )
    await sched.start()
    try:
        for _ in range(50):
            if (await repo.get_task(task.id)).status == "completed":
                break
            await asyncio.sleep(0.02)
    finally:
        await sched.stop()

    assert (await repo.get_task(task.id)).status == "completed"
