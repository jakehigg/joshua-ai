"""Start the work the person means: drafts_open, the hint of each refusal, a
cancel that frees the slot before it returns, and a delete that a restore undoes."""

from __future__ import annotations

import asyncio

import pytest
from conftest import ALEX, MIA, VIEWER, error, mcp_session, mcp_sessions, payload, spec
from fakes import FIXED_NOW
from joshua_assignments import manager as manager_mod
from joshua_assignments.server import get_manager, mcp
from test_drafts import make_config_off
from test_manager import make_manager
from test_read_api import call

# --- drafts_open -------------------------------------------------------------


async def test_a_new_spec_is_refused_while_the_person_has_drafts(channels) -> None:
    manager = make_manager(delay=None)
    first = await manager.draft("alex", spec(title="Docs drift"))
    second = await manager.draft("alex", spec(title="Wiki tidy"))
    refused = await manager.start("alex", spec(title="Docs drift, again"))
    assert refused["status"] == "rejected" and refused["reason"] == "drafts_open"
    assert refused["drafts"] == [
        {"id": second["id"], "title": "Wiki tidy"},
        {"id": first["id"], "title": "Docs drift"},
    ]
    assert refused["hint"] == (
        "Start the draft by id, or pass new: true for a job none of these covers."
    )
    assert sorted(manager.store.rows) == [first["id"], second["id"]]
    assert all(row["status"] == "draft" for row in manager.store.rows.values())


async def test_new_true_starts_a_spec_beside_the_drafts(channels) -> None:
    manager = make_manager(delay=None)
    drafted = await manager.draft("alex", spec())
    started = await manager.start("alex", spec(title="Another job"), new=True)
    assert started["status"] == "running" and started["id"] != drafted["id"]
    assert manager.store.rows[drafted["id"]]["status"] == "draft"


async def test_drafts_open_counts_only_the_drafts_of_the_person(channels) -> None:
    manager = make_manager(delay=None)
    await manager.draft("mia", spec())
    gone = await manager.draft("alex", spec())
    await manager.delete_draft(manager.store.rows[gone["id"]])
    assert (await manager.start("alex", spec()))["status"] == "running"


async def test_a_saved_start_and_a_draft_start_are_not_drafts_open(channels) -> None:
    manager = make_manager()
    await manager.save("alex", "docs", spec())
    drafted = await manager.draft("alex", spec())
    other = await manager.draft("alex", spec(title="Other"))
    assert (await manager.start("alex", saved="docs"))["status"] == "running"
    await manager.runner.idle()
    by_id = await manager.start("alex", draft=manager.store.rows[drafted["id"]])
    assert by_id["status"] == "running" and by_id["id"] == drafted["id"]
    assert manager.store.rows[other["id"]]["status"] == "draft"


# --- the hint of each refusal ------------------------------------------------


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"raw_spec": {"goal": ""}, "new": True}, "invalid_spec"),
        ({"raw_spec": {"goal": "x", "model": "gpt"}, "new": True}, "unknown_model"),
        ({"saved": "nothing-here"}, "not_found"),
        ({}, "invalid_arguments"),
    ],
)
async def test_every_refusal_of_a_start_has_a_hint(channels, args, reason) -> None:
    manager = make_manager()
    refused = await manager.start("alex", **args)
    assert refused["reason"] == reason
    assert isinstance(refused["hint"], str) and refused["hint"].endswith(".")


async def test_a_start_of_a_run_by_id_says_it_is_not_a_draft(channels) -> None:
    manager = make_manager()
    started = await manager.start("alex", spec())
    await manager.runner.idle()
    refused = await manager.start("alex", draft=manager.store.rows[started["id"]])
    assert refused["reason"] == "not_a_draft"
    assert refused["hint"].startswith(f"Assignment {started['id']} is done, not a draft")


async def test_a_disabled_start_has_a_hint(channels) -> None:
    manager = make_manager()
    manager.load_config = make_config_off
    refused = await manager.start("alex", spec())
    assert refused["reason"] == "disabled" and refused["hint"]


async def test_busy_gives_the_working_time_left_as_the_retry(channels) -> None:
    manager = make_manager(delay=None)
    first = await manager.start("alex", spec(working_time="2h"))
    await manager.store.update(first["id"], working_s=1800)
    refused = await manager.start("alex", spec())
    # 5400 seconds are left, and the retry is at most one hour.
    assert refused["retry_after_s"] == manager_mod.BUSY_RETRY_MAX_S
    await manager.store.update(first["id"], working_s=7000)
    refused = await manager.start("alex", spec())
    assert refused["retry_after_s"] == manager_mod.BUSY_RETRY_MIN_S


# --- cancel frees the slot ---------------------------------------------------


async def test_cancel_returns_after_the_slot_is_free(channels) -> None:
    manager = make_manager(delay=None)
    running = await manager.start("alex", spec())
    stopping = asyncio.Event()
    stop = manager.runner.cancel

    async def slow_cancel(row) -> None:
        stopping.set()
        await asyncio.sleep(0.05)
        await stop(row)

    manager.runner.cancel = slow_cancel  # type: ignore[method-assign]
    cancel = asyncio.create_task(manager.cancel(manager.store.rows[running["id"]]))
    await stopping.wait()
    # A start while the session still stops waits for the cancel, and is not busy.
    started = await manager.start("alex", spec())
    cancelled = await cancel
    assert cancelled["status"] == "cancelled" and "slot is free" in cancelled["message"]
    assert started["status"] == "running"
    assert manager.store.rows[running["id"]]["status"] == "cancelled"
    assert [row["id"] for row in await manager.store.running()] == [started["id"]]


async def test_a_start_right_after_a_cancel_is_not_busy(channels) -> None:
    manager = make_manager(delay=None)
    running = await manager.start("alex", spec())
    await manager.cancel(manager.store.rows[running["id"]])
    assert await manager.store.running() == []
    assert (await manager.start("alex", spec()))["status"] == "running"


async def test_cancel_refuses_a_run_that_ended_meanwhile(channels) -> None:
    manager = make_manager(delay=None)
    running = await manager.start("alex", spec())
    row = manager.store.rows[running["id"]].copy()
    await manager.store.update(running["id"], status="done", finished_at=FIXED_NOW)
    refused = await manager.cancel(row)
    assert refused["reason"] == "finished"
    assert manager.store.rows[running["id"]]["status"] == "done"


# --- over MCP ----------------------------------------------------------------


async def test_the_start_tool_leads_with_the_draft_id() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    start = " ".join((tools["start_assignment"].description or "").split())
    assert start.startswith(
        "To start a draft, pass its id. Pass spec only for a new job that no draft "
        "covers. Pass saved to run a saved assignment."
    )
    assert (tools["delete_assignment"].description or "").startswith(
        "Removes a draft from view; the person can ask for it back."
    )
    assert "new" in tools["start_assignment"].input_schema["properties"]
    assert "restore_assignment" in tools


async def test_a_started_draft_keeps_its_id_over_mcp(app, store) -> None:
    async with mcp_session(app) as session:
        drafted = payload(await session.call_tool("draft_assignment", {"spec": spec()}))
        refused = payload(await session.call_tool("start_assignment", {"spec": spec()}))
        started = payload(await session.call_tool("start_assignment", {"id": drafted["id"]}))
        status = payload(await session.call_tool("assignment_status", {"id": drafted["id"]}))
    assert refused["reason"] == "drafts_open"
    assert refused["drafts"] == [{"id": drafted["id"], "title": "Audit the gitops repo"}]
    assert started["id"] == drafted["id"]
    assert status["id"] == drafted["id"] and status["state"] != "draft"
    assert list(store.rows) == [drafted["id"]]


async def test_new_true_over_mcp(app, store) -> None:
    async with mcp_session(app) as session:
        await session.call_tool("draft_assignment", {"spec": spec()})
        started = payload(
            await session.call_tool("start_assignment", {"spec": spec(), "new": True})
        )
    assert started["status"] == "running"
    assert len(store.rows) == 2


async def test_delete_and_restore_over_mcp(app, store) -> None:
    async with mcp_session(app) as session:
        drafted = payload(await session.call_tool("draft_assignment", {"spec": spec()}))
        draft_id = drafted["id"]
        deleted = payload(await session.call_tool("delete_assignment", {"id": draft_id}))
        listed = payload(await session.call_tool("list_assignments", {}))
        status = await session.call_tool("assignment_status", {"id": draft_id})
        update = await session.call_tool("update_assignment", {"id": draft_id, "spec": spec()})
        start = payload(await session.call_tool("start_assignment", {"id": draft_id}))
        restored = payload(await session.call_tool("restore_assignment", {"id": draft_id}))
        again = payload(await session.call_tool("restore_assignment", {"id": draft_id}))
        back = payload(await session.call_tool("list_assignments", {"state": "draft"}))
    assert deleted["status"] == "deleted"
    assert listed["assignments"] == []
    assert error(status).endswith(f"404 not found: no assignment {draft_id}")
    assert error(update).endswith(f"404 not found: no assignment {draft_id}")
    assert start["reason"] == "deleted"
    assert f"restore_assignment({draft_id})" in start["hint"]
    assert restored["status"] == "draft" and restored["id"] == draft_id
    assert again["reason"] == "not_deleted"
    assert [a["id"] for a in back["assignments"]] == [draft_id]
    assert store.rows[draft_id]["status"] == "draft"


async def test_another_person_cannot_restore_a_draft(app, store) -> None:
    async with mcp_sessions(app, ALEX, MIA) as (alex, mia):
        drafted = payload(await alex.call_tool("draft_assignment", {"spec": spec()}))
        await alex.call_tool("delete_assignment", {"id": drafted["id"]})
        result = await mia.call_tool("restore_assignment", {"id": drafted["id"]})
        start = await mia.call_tool("start_assignment", {"id": drafted["id"]})
    assert error(result).endswith(f"404 not found: no assignment {drafted['id']}")
    assert error(start).endswith(f"404 not found: no assignment {drafted['id']}")
    assert store.rows[drafted["id"]]["deleted_at"] is not None


async def test_the_busy_refusal_over_mcp_hides_another_person(holding_app, store) -> None:
    async with mcp_sessions(holding_app, ALEX, MIA) as (alex, mia):
        first = payload(await mia.call_tool("start_assignment", {"spec": spec()}))
        refused = payload(await alex.call_tool("start_assignment", {"spec": spec()}))
    assert refused["reason"] == "busy"
    assert refused["running"] == {"id": None, "title": "another person's assignment"}
    assert str(first["id"]) not in refused["hint"]
    assert "Audit" not in refused["hint"] + refused["message"]


# --- the read API ------------------------------------------------------------


async def test_the_read_api_hides_a_deleted_draft(app, store) -> None:
    manager = get_manager()
    kept = await manager.draft("alex", spec(title="Kept"))
    gone = await manager.draft("alex", spec(title="Gone"))
    await manager.delete_draft(store.rows[gone["id"]])
    listed = await call(app, "/v1/assignments", person="alex")
    drafts = await call(app, "/v1/assignments", person="alex", state="draft")
    detail = await call(app, f"/v1/assignments/{gone['id']}", person="alex")
    comments = await call(app, f"/v1/assignments/{gone['id']}/comments", person="alex")
    assert [i["id"] for i in listed.json()["items"]] == [kept["id"]]
    assert [i["id"] for i in drafts.json()["items"]] == [kept["id"]]
    assert detail.status_code == 404 and comments.status_code == 404
    await manager.restore(store.rows[gone["id"]])
    detail = await call(app, f"/v1/assignments/{gone['id']}", person="alex", token=VIEWER)
    assert detail.status_code == 200 and detail.json()["title"] == "Gone"
