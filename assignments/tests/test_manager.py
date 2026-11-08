"""The manager rules: the queue, the slots, a restart, and each refusal."""

from __future__ import annotations

import pytest
from conftest import make_config, make_settings, spec
from fakes import FIXED_NOW, FakeStore
from joshua_assignments.manager import Manager, full_view, summary_view


def make_manager(
    extra: str = "", delay: float | None = 0.0, store: FakeStore | None = None
) -> Manager:
    cfg = make_config(extra)
    return Manager(
        make_settings(),
        store or FakeStore(),
        load_config=lambda: cfg,
        clock=lambda: FIXED_NOW,
        stub_delay_s=delay,
    )


async def test_max_concurrent_keeps_the_rest_queued(channels) -> None:
    manager = make_manager(delay=None)
    first = await manager.start("alex", spec())
    second = await manager.start("alex", spec())
    assert first["status"] == "running"
    assert second["status"] == "queued"
    await manager.cancel(await manager.store.get(first["id"]))
    assert (await manager.store.get(second["id"]))["status"] == "running"


async def test_a_finished_run_starts_the_next_one(channels) -> None:
    manager = make_manager()
    await manager.start("alex", spec())
    await manager.start("alex", spec())
    await manager.runner.idle()
    statuses = [row["status"] for row in manager.store.rows.values()]
    assert statuses == ["done", "done"]
    assert len(channels.requests) == 2


async def test_two_slots_run_two(channels) -> None:
    manager = make_manager("  max_concurrent: 2\n", delay=None)
    a = await manager.start("alex", spec())
    b = await manager.start("alex", spec())
    assert (a["status"], b["status"]) == ("running", "running")


async def test_a_restart_puts_running_assignments_back_and_resumes_them(channels) -> None:
    store = FakeStore()
    first = make_manager(delay=None, store=store)
    started = await first.start("alex", spec())
    assert store.rows[started["id"]]["status"] == "running"
    second = make_manager(store=store)
    resumed: list[tuple[int, str]] = []
    real_resume = second.runner.resume

    async def spy(row, text):
        resumed.append((row["id"], text))
        await real_resume(row, text)

    second.runner.resume = spy  # type: ignore[method-assign]
    await second.open()
    await second.runner.idle()
    assert store.opened is True
    assert resumed == [(started["id"], "")]
    assert store.rows[started["id"]]["status"] == "done"
    await second.close()
    assert store.closed is True


async def test_a_parked_stop_sends_the_question(channels) -> None:
    manager = make_manager(delay=None)
    started = await manager.start("alex", spec())
    await manager.runner.cancel({"id": started["id"]})
    await manager.store.update(started["id"], status="waiting", open_question="Which project?")
    await manager.stopped(started["id"])
    [body] = channels.bodies
    assert "has stopped and has a question" in body["text"]
    assert body["text"].endswith("```\nWhich project?\n```")


async def test_stopped_for_an_unknown_id_sends_nothing(channels) -> None:
    manager = make_manager()
    await manager.stopped(42)
    assert channels.requests == []


async def test_start_refuses_when_disabled(channels) -> None:
    manager = make_manager()
    manager.load_config = lambda: make_config(text=CONFIG_OFF)
    result = await manager.start("alex", spec())
    assert result["reason"] == "disabled"


CONFIG_OFF = """
name: Home
timezone: UTC
people:
  - id: alex
    name: Alex
"""


@pytest.mark.parametrize(
    ("raw", "reason"),
    [("goal", "invalid_spec"), ({"goal": ""}, "invalid_spec"), ({"nope": 1}, "invalid_spec")],
)
async def test_start_refuses_a_bad_spec(channels, raw, reason) -> None:
    manager = make_manager()
    result = await manager.start("alex", raw)
    assert result["status"] == "rejected" and result["reason"] == reason
    assert manager.store.rows == {}


async def test_a_long_conversation_id_is_cut(channels) -> None:
    manager = make_manager(delay=None)
    await manager.start("alex", spec(), "c" * 500)
    assert manager.store.rows[1]["conversation_id"] == "c" * 200


async def test_recent_filters_and_clamps(channels) -> None:
    manager = make_manager(delay=None)
    await manager.start("alex", spec())
    await manager.start("alex", spec())
    await manager.start("mia", spec())
    assert len((await manager.recent("alex", 0, None))["assignments"]) == 1
    assert [a["id"] for a in (await manager.recent("alex", 500, "queued"))["assignments"]] == [2]
    assert len((await manager.recent(None, 10, None))["assignments"]) == 3
    assert (await manager.recent("alex", 10, "bogus"))["reason"] == "invalid_status"


async def test_answer_refusals(channels) -> None:
    manager = make_manager(delay=None)
    started = await manager.start("alex", spec())
    row = await manager.store.get(started["id"])
    assert (await manager.answer(row, " "))["reason"] == "invalid_arguments"
    assert (await manager.answer(row, "x" * 20001))["reason"] == "invalid_arguments"
    assert (await manager.answer(row, "yes"))["reason"] == "not_waiting"


async def test_answer_resumes_a_paused_assignment_with_the_answer(channels) -> None:
    manager = make_manager(delay=None)
    started = await manager.start("alex", spec())
    await manager.runner.cancel({"id": started["id"]})
    await manager.store.update(
        started["id"], status="paused_limits", stopped_at_limits={"five_hour": 80}
    )
    resumed: list[str] = []

    async def spy(row, text):
        resumed.append(text)

    manager.runner.resume = spy  # type: ignore[method-assign]
    result = await manager.answer(await manager.store.get(started["id"]), "continue")
    assert result["status"] == "running"
    assert resumed == ["continue"]
    stored = manager.store.rows[started["id"]]
    assert stored["stopped_at_limits"] is None and stored["answer"] == "continue"


async def test_cancel_of_a_finished_assignment_is_refused(channels) -> None:
    manager = make_manager()
    started = await manager.start("alex", spec())
    await manager.runner.idle()
    result = await manager.cancel(await manager.store.get(started["id"]))
    assert result["reason"] == "finished"


async def test_extend_raises_caps_within_the_maxima(channels) -> None:
    manager = make_manager(delay=None)
    started = await manager.start("alex", spec())
    row = await manager.store.get(started["id"])
    result = await manager.extend(
        row, tokens=100, working_time="6h", five_hour_pct=99, seven_day_pct=90
    )
    assert result["budget"] == {"five_hour_pct": 90, "seven_day_pct": 90, "tokens": 100}
    assert result["working_time"] == "6h"
    assert "budget.five_hour_pct 99 is above the maximum 90, so it is 90" in result["message"]
    assert manager.store.rows[started["id"]]["spec"]["working_time"] == "6h"


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({}, "invalid_arguments"),
        ({"tokens": 0}, "not_raised"),
        ({"working_time": "soon"}, "invalid_arguments"),
        ({"working_time": "1h"}, "not_raised"),
        ({"five_hour_pct": 50}, "not_raised"),
        ({"seven_day_pct": 85}, "not_raised"),
    ],
)
async def test_extend_refusals(channels, args, reason) -> None:
    manager = make_manager(delay=None)
    started = await manager.start("alex", spec(budget={"tokens": 10}))
    row = await manager.store.get(started["id"])
    assert (await manager.extend(row, **args))["reason"] == reason
    if "tokens" in args:
        assert (await manager.extend(row, tokens=5))["reason"] == "not_raised"


async def test_extend_of_a_finished_assignment_is_refused(channels) -> None:
    manager = make_manager()
    started = await manager.start("alex", spec())
    await manager.runner.idle()
    row = await manager.store.get(started["id"])
    assert (await manager.extend(row, tokens=5))["reason"] == "finished"


def test_is_member() -> None:
    manager = make_manager()
    assert manager.is_member("alex") is True
    assert manager.is_member("sam") is False
    assert manager.is_member("nobody") is False


def test_the_views_of_a_bare_row() -> None:
    row = {"id": 1, "person_id": "alex", "status": "queued"}
    assert summary_view(row)["goal"] == ""
    view = full_view(row, None)
    assert view["last_checkpoint"] is None
    assert view["usage"] == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    assert view["repos"] == []
