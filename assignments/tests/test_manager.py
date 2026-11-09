"""The manager rules: the slots, no queue, a restart, and each refusal."""

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


async def ended(manager: Manager, person: str = "alex", **fields) -> dict:
    """Start an assignment, then end it at once with ``fields``, so the slot is free."""
    started = await manager.start(person, spec())
    await manager.runner.cancel({"id": started["id"]})
    await manager.store.update(started["id"], **({"status": "done"} | fields))
    return started


async def test_a_start_while_busy_is_refused_and_stores_nothing(channels) -> None:
    manager = make_manager(delay=None)
    first = await manager.start("alex", spec())
    second = await manager.start("alex", spec())
    assert first["status"] == "running"
    assert second["status"] == "rejected" and second["reason"] == "busy"
    assert second["running"] == {"id": first["id"], "title": "Audit the gitops repo"}
    assert f"assignment {first['id']} is running" in second["message"]
    assert second["hint"] == (
        f"Assignment {first['id']} is still running; start this one when it ends, "
        "do not create another."
    )
    assert isinstance(second["retry_after_s"], int)
    assert list(manager.store.rows) == [first["id"]]
    await manager.cancel(await manager.store.get(first["id"]))
    third = await manager.start("alex", spec())
    assert third["status"] == "running"


async def test_busy_does_not_name_the_assignment_of_another_person(channels) -> None:
    manager = make_manager(delay=None)
    await manager.start("mia", spec())
    refused = await manager.start("alex", spec())
    assert refused["reason"] == "busy"
    assert refused["running"] == {"id": None, "title": "another person's assignment"}
    assert "another person" in refused["message"]
    assert refused["hint"] == (
        "Another person's assignment is still running; start this one when it ends, "
        "do not create another."
    )
    assert refused["retry_after_s"] == 900
    # Nothing in the refusal names the run of the other person.
    assert "assignment 1" not in refused["hint"] + refused["message"]


async def test_a_finished_run_starts_nothing(channels) -> None:
    manager = make_manager()
    await manager.start("alex", spec())
    await manager.runner.idle()
    await manager.draft("alex", spec())
    await manager.stopped(1)
    assert [row["status"] for row in manager.store.rows.values()] == ["done", "draft"]
    assert not hasattr(manager, "dispatch")
    assert not hasattr(manager.store, "next_queued")


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
    resumed: list[tuple[int, str, str]] = []
    real_resume = second.runner.resume

    async def spy(row, text, *, reason="answer"):
        resumed.append((row["id"], text, reason))
        await real_resume(row, text, reason=reason)

    second.runner.resume = spy  # type: ignore[method-assign]
    await second.open()
    await second.runner.idle()
    assert store.opened is True
    assert resumed == [(started["id"], "", "restart")]
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
    await ended(manager)
    await manager.draft("alex", spec())
    await ended(manager, "mia")
    assert len((await manager.recent("alex", 0, None))["assignments"]) == 1
    assert [a["id"] for a in (await manager.recent("alex", 500, "draft"))["assignments"]] == [2]
    assert len((await manager.recent(None, 10, None))["assignments"]) == 3
    assert (await manager.recent("alex", 10, "bogus"))["reason"] == "invalid_status"


async def test_recent_filters_by_state_and_gives_title_and_state(channels) -> None:
    manager = make_manager("  max_concurrent: 3\n", delay=None)
    first = await manager.start("alex", spec(title="Weekly audit"))
    second = await manager.start("alex", spec())
    await manager.store.update(second["id"], status="waiting")
    third = await manager.start("alex", spec())
    await manager.store.update(third["id"], status="failed")
    fourth = await manager.draft("alex", spec(title="Later"))

    def ids(reply: dict) -> list[int]:
        return [a["id"] for a in reply["assignments"]]

    assert ids(await manager.recent("alex", 10, None, "in_progress")) == [second["id"], first["id"]]
    assert ids(await manager.recent("alex", 10, None, "completed")) == [third["id"]]
    assert ids(await manager.recent("alex", 10, None, "draft")) == [fourth["id"]]
    assert (await manager.recent("alex", 10, None, "queued"))["reason"] == "invalid_state"
    assert ids(await manager.recent("alex", 10, "waiting", "in_progress")) == [second["id"]]
    assert (await manager.recent("alex", 10, None, "done"))["reason"] == "invalid_state"
    listed = (await manager.recent("alex", 10, None))["assignments"]
    assert [(a["title"], a["state"]) for a in listed] == [
        ("Later", "draft"),
        ("Audit the gitops repo", "completed"),
        ("Audit the gitops repo", "in_progress"),
        ("Weekly audit", "in_progress"),
    ]
    assert all("page_path" not in a for a in listed)


async def test_status_gives_the_plan_the_report_and_the_outputs(channels) -> None:
    manager = make_manager(delay=None)
    started = await manager.start("alex", spec())
    assert started["title"] == "Audit the gitops repo" and "page_path" not in started
    store = manager.store
    await store.write_plan(started["id"], "# Plan")
    await store.add_output(started["id"], "issue", "ops#1", "First")
    await store.write_report(started["id"], "# Audit")
    status = await manager.status(await store.get(started["id"]))
    assert status["state"] == "in_progress" and status["title"] == "Audit the gitops repo"
    assert status["plan"] == "# Plan" and status["report"] == "# Audit"
    assert status["report_at"] == FIXED_NOW.isoformat()
    assert [o["ref"] for o in status["outputs"]] == ["ops#1"]


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

    async def spy(row, text, *, reason="answer"):
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
    row = {"id": 1, "person_id": "alex", "status": "draft"}
    assert summary_view(row)["goal"] == ""
    view = full_view(row, None)
    assert view["last_checkpoint"] is None
    assert view["usage"] == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    assert view["repos"] == []


async def test_a_failed_sweep_never_stops_a_start(channels) -> None:
    from joshua_assignments.runner import StubRunner

    class Sweeping(StubRunner):
        async def sweep(self) -> None:
            raise OSError("disk")

    cfg = make_config()
    manager = Manager(
        make_settings(),
        FakeStore(),
        load_config=lambda: cfg,
        clock=lambda: FIXED_NOW,
        runner=lambda m: Sweeping(m),
    )
    await manager.open()
    started = await manager.start("alex", spec())
    await manager.runner.idle()
    assert manager.store.rows[started["id"]]["status"] == "done"


def test_the_status_gives_the_next_step_of_the_last_checkpoint() -> None:
    row = {"id": 1, "person_id": "alex", "status": "running", "spec": {"goal": "g"}}
    view = full_view(row, {"at": FIXED_NOW, "note": "plan written. Next: clone it"})
    assert view["next_step"] == "clone it"
    assert full_view(row, None)["next_step"] is None
