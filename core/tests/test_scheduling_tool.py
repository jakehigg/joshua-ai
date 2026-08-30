"""Unit tests for the ``scheduling`` builtin tool functions (offline, no SDK)."""

from __future__ import annotations

from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.scheduling import (
    build_scheduling_server,
    do_cancel_task,
    do_list_tasks,
    do_pause_task,
    do_resume_task,
    do_schedule_task,
    do_update_task,
    register,
)
from joshua_core.store.models import Channel, Conversation
from scheduler_fakes import FakeTaskRepo

TZ = "America/New_York"


def _deps(repo: FakeTaskRepo, conversation_id: str = "c1") -> ToolDeps:
    conv = Conversation(id=conversation_id, channel_id="telegram:1", person_id="alex")
    channel = Channel(id="telegram:1", channel_type="telegram")
    return ToolDeps(repo=repo, conversation=conv, channel=channel, tz=TZ, person_id="alex")


def _text(reply: dict) -> str:
    return reply["content"][0]["text"]


async def test_schedule_in_seconds_creates_one_shot() -> None:
    repo = FakeTaskRepo()
    deps = _deps(repo)
    reply = await do_schedule_task(deps, prompt="stand up", in_seconds=60)
    assert "One-time task" in _text(reply)
    tasks = await repo.list_tasks("c1")
    assert len(tasks) == 1
    assert tasks[0].prompt == "stand up"
    assert tasks[0].cron_expr is None


async def test_schedule_cron_creates_recurring() -> None:
    repo = FakeTaskRepo()
    reply = await do_schedule_task(_deps(repo), prompt="weekly review", cron="0 18 * * 4")
    assert "Recurring" in _text(reply)
    task = (await repo.list_tasks("c1"))[0]
    assert task.cron_expr == "0 18 * * 4"


async def test_schedule_requires_prompt_and_time() -> None:
    repo = FakeTaskRepo()
    assert "instruction" in _text(await do_schedule_task(_deps(repo), prompt="  "))
    assert "could not schedule" in _text(await do_schedule_task(_deps(repo), prompt="do it"))
    assert await repo.list_tasks("c1") == []


async def test_schedule_bad_cron_reports_error() -> None:
    repo = FakeTaskRepo()
    reply = await do_schedule_task(_deps(repo), prompt="x", cron="not a cron")
    assert "could not schedule" in _text(reply)
    assert await repo.list_tasks("c1") == []


async def test_list_tasks_only_this_conversation() -> None:
    repo = FakeTaskRepo()
    await do_schedule_task(_deps(repo, "c1"), prompt="mine", in_seconds=30)
    await do_schedule_task(_deps(repo, "c2"), prompt="theirs", in_seconds=30)
    listing = _text(await do_list_tasks(_deps(repo, "c1")))
    assert "mine" in listing
    assert "theirs" not in listing


async def test_list_tasks_empty() -> None:
    assert "no scheduled tasks" in _text(await do_list_tasks(_deps(FakeTaskRepo())))


async def test_cancel_pause_resume_ownership() -> None:
    repo = FakeTaskRepo()
    await do_schedule_task(_deps(repo, "c1"), prompt="mine", cron="*/5 * * * *")
    task_id = (await repo.list_tasks("c1"))[0].id

    # Another conversation cannot touch it.
    assert "no task" in _text(await do_cancel_task(_deps(repo, "c2"), task_id=task_id))
    assert (await repo.get_task(task_id)).status == "active"

    assert "Paused" in _text(await do_pause_task(_deps(repo, "c1"), task_id=task_id))
    assert (await repo.get_task(task_id)).status == "paused"
    assert "Resumed" in _text(await do_resume_task(_deps(repo, "c1"), task_id=task_id))
    assert (await repo.get_task(task_id)).status == "active"
    assert "Cancelled" in _text(await do_cancel_task(_deps(repo, "c1"), task_id=task_id))
    assert (await repo.get_task(task_id)).status == "completed"


async def test_update_task_changes_prompt_and_schedule() -> None:
    repo = FakeTaskRepo()
    await do_schedule_task(_deps(repo, "c1"), prompt="old", in_seconds=30)
    task_id = (await repo.list_tasks("c1"))[0].id

    reply = await do_update_task(_deps(repo, "c1"), task_id=task_id, prompt="new", cron="0 9 * * *")
    assert "Updated" in _text(reply)
    task = await repo.get_task(task_id)
    assert task.prompt == "new"
    assert task.cron_expr == "0 9 * * *"


async def test_update_to_one_time_clears_cron() -> None:
    repo = FakeTaskRepo()
    await do_schedule_task(_deps(repo, "c1"), prompt="weekly", cron="0 9 * * 1")
    task_id = (await repo.list_tasks("c1"))[0].id

    await do_update_task(_deps(repo, "c1"), task_id=task_id, when="in 1 hour")

    task = await repo.get_task(task_id)
    assert task.cron_expr is None  # no longer recurring


async def test_update_task_needs_a_change() -> None:
    repo = FakeTaskRepo()
    await do_schedule_task(_deps(repo, "c1"), prompt="x", in_seconds=30)
    task_id = (await repo.list_tasks("c1"))[0].id
    assert "Tell me what to change" in _text(
        await do_update_task(_deps(repo, "c1"), task_id=task_id)
    )


def test_build_scheduling_server() -> None:
    # Needs the SDK (create_sdk_mcp_server); runs in CI where it is installed.
    server = build_scheduling_server(_deps(FakeTaskRepo()))
    assert server["type"] == "sdk"
    assert server["name"] == "scheduling"


def test_register_wires_the_builtin() -> None:
    wired: dict[str, object] = {}
    register(type("M", (), {"register_builtin": lambda self, n, f: wired.__setitem__(n, f)})())
    assert "scheduling" in wired
