"""Drafts and no queue: draft, update, start by id, busy, delete, and comments on a draft."""

from __future__ import annotations

import pytest
from conftest import ALEX, MIA, error, mcp_session, mcp_sessions, payload, spec
from fakes import FIXED_NOW, FakeStore
from joshua_assignments import definition
from joshua_assignments.server import get_manager
from joshua_assignments.store import Parent
from test_comments import post
from test_manager import CONFIG_OFF, make_manager


async def test_a_draft_is_stored_and_never_runs(channels) -> None:
    manager = make_manager()
    started: list[int] = []

    async def spy(row) -> None:
        started.append(row["id"])

    manager.runner.start = spy  # type: ignore[method-assign]
    drafted = await manager.draft("alex", spec(budget={"five_hour_pct": 99}), "conv-1")
    row = manager.store.rows[drafted["id"]]
    assert drafted["status"] == "draft" and row["status"] == "draft"
    assert drafted["title"] == "Audit the gitops repo"
    assert drafted["notes"] == ["budget.five_hour_pct 99 is above the maximum 90, so it is 90"]
    assert f"start_assignment(id={drafted['id']})" in drafted["message"]
    assert row["started_at"] is None and row["conversation_id"] == "conv-1"
    assert row["spec"]["model"] == "sonnet" and row["spec"]["budget"]["five_hour_pct"] == 90
    await manager.stopped(drafted["id"])
    assert started == [] and channels.requests == []
    assert (await manager.status(row))["state"] == "draft"


async def test_a_draft_takes_no_slot(channels) -> None:
    manager = make_manager(delay=None)
    await manager.draft("alex", spec())
    await manager.draft("alex", spec())
    assert (await manager.start("alex", spec(), new=True))["status"] == "running"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [({"goal": ""}, "invalid_spec"), ({"goal": "x", "model": "gpt"}, "unknown_model")],
)
async def test_a_bad_draft_is_refused(channels, raw, reason) -> None:
    manager = make_manager()
    assert (await manager.draft("alex", raw))["reason"] == reason
    assert (await manager.draft("alex"))["reason"] == "invalid_arguments"
    assert manager.store.rows == {}


async def test_a_draft_is_refused_when_disabled(channels) -> None:
    manager = make_manager()
    manager.load_config = lambda: make_config_off()
    assert (await manager.draft("alex", spec()))["reason"] == "disabled"
    assert (await manager.update_draft({"id": 1, "status": "draft"}, spec()))["reason"] == (
        "disabled"
    )


def make_config_off():
    from conftest import make_config

    return make_config(text=CONFIG_OFF)


async def test_a_draft_from_a_saved_assignment_names_it(channels) -> None:
    manager = make_manager()
    saved = await manager.save("alex", "docs", spec(title="Docs"))
    drafted = await manager.draft("alex", {"model": "opus"}, saved="docs")
    row = manager.store.rows[drafted["id"]]
    assert drafted["saved"] == {"id": saved["id"], "name": "docs"}
    assert row["saved_id"] == saved["id"] and row["spec"]["model"] == "opus"
    assert row["spec"]["title"] == "Docs"
    assert (await manager.list_saved("alex"))["saved"][0]["runs"] == 0
    assert (await manager.draft("alex", saved="nothing-here"))["reason"] == "not_found"


async def test_update_replaces_the_spec_of_a_draft(channels) -> None:
    manager = make_manager()
    drafted = await manager.draft("alex", spec())
    row = manager.store.rows[drafted["id"]]
    updated = await manager.update_draft(row, spec(title="Shorter", model="opus"))
    stored = manager.store.rows[drafted["id"]]
    assert updated["status"] == "draft" and updated["title"] == "Shorter"
    assert stored["spec"]["title"] == "Shorter" and stored["spec"]["model"] == "opus"
    assert stored["model"] == make_manager().section.models["opus"].model
    assert (updated["open_comments"], updated["stale_comments"]) == (0, 0)
    bad = await manager.update_draft(stored, {"goal": ""})
    assert bad["reason"] == "invalid_spec"
    assert manager.store.rows[drafted["id"]]["spec"]["title"] == "Shorter"


@pytest.mark.parametrize("status", ["running", "waiting", "done", "cancelled"])
async def test_update_and_delete_refuse_a_run(channels, status) -> None:
    manager = make_manager()
    drafted = await manager.draft("alex", spec())
    await manager.store.update(drafted["id"], status=status, started_at=FIXED_NOW)
    row = manager.store.rows[drafted["id"]]
    assert (await manager.update_draft(row, spec(title="New")))["reason"] == "not_a_draft"
    assert (await manager.delete_draft(row))["reason"] == "not_a_draft"
    assert manager.store.rows[drafted["id"]]["spec"]["title"] == "Audit the gitops repo"


async def test_delete_hides_a_draft_and_restore_brings_it_back(channels) -> None:
    manager = make_manager()
    drafted = await manager.draft("alex", spec())
    row = manager.store.rows[drafted["id"]]
    await manager.store.add_comment(
        "alex",
        Parent("assignment", drafted["id"]),
        target="definition",
        line_no=1,
        line_text="x",
        body="Shorter.",
    )
    deleted = await manager.delete_draft(row)
    assert deleted["status"] == "deleted"
    assert f"restore_assignment({drafted['id']})" in deleted["message"]
    # The row and its comment stay; the row is in no list and in no count.
    assert manager.store.rows[drafted["id"]]["deleted_at"] == FIXED_NOW
    assert len(manager.store.comment_rows) == 1
    assert (await manager.recent("alex", 10, None))["assignments"] == []
    assert (await manager.recent("alex", 10, None, "draft"))["assignments"] == []
    assert await manager.store.counts() == {}
    hidden = manager.store.rows[drafted["id"]]
    assert (await manager.delete_draft(hidden))["reason"] == "not_a_draft"
    restored = await manager.restore(hidden)
    assert restored["status"] == "draft" and restored["id"] == drafted["id"]
    assert f"start_assignment(id={drafted['id']})" in restored["message"]
    assert manager.store.rows[drafted["id"]]["deleted_at"] is None
    listed = (await manager.recent("alex", 10, None, "draft"))["assignments"]
    assert [a["id"] for a in listed] == [drafted["id"]]
    assert (await manager.restore(manager.store.rows[drafted["id"]]))["reason"] == ("not_deleted")


async def test_start_by_id_runs_a_draft(channels) -> None:
    manager = make_manager(delay=None)
    drafted = await manager.draft("alex", spec())
    started = await manager.start("alex", draft=manager.store.rows[drafted["id"]])
    row = manager.store.rows[drafted["id"]]
    assert started["id"] == drafted["id"] and started["status"] == "running"
    assert row["status"] == "running" and row["started_at"] == FIXED_NOW
    again = await manager.start("alex", draft=row)
    assert again["reason"] == "not_a_draft"


async def test_start_by_id_checks_the_spec_again(channels) -> None:
    manager = make_manager()
    drafted = await manager.draft("alex", spec())
    await manager.store.update(drafted["id"], spec=spec(model="gpt"))
    refused = await manager.start("alex", draft=manager.store.rows[drafted["id"]])
    assert refused["reason"] == "unknown_model"
    assert manager.store.rows[drafted["id"]]["status"] == "draft"


async def test_start_takes_one_of_spec_id_and_saved(channels) -> None:
    manager = make_manager()
    drafted = await manager.draft("alex", spec())
    row = manager.store.rows[drafted["id"]]
    assert (await manager.start("alex", spec(), draft=row))["reason"] == "invalid_arguments"
    assert (await manager.start("alex", saved="docs", draft=row))["reason"] == "invalid_arguments"


async def test_a_draft_of_an_earlier_queue_that_ran_resumes_its_session(channels) -> None:
    manager = make_manager(delay=None)
    drafted = await manager.draft("alex", spec())
    await manager.store.update(drafted["id"], started_at=FIXED_NOW, answer="Use ops.")
    resumed: list[tuple[str, str]] = []

    async def spy(row, text, *, reason="answer"):
        resumed.append((text, reason))

    manager.runner.resume = spy  # type: ignore[method-assign]
    started = await manager.start("alex", draft=manager.store.rows[drafted["id"]])
    assert started["status"] == "running"
    assert resumed == [("Use ops.", "answer")]
    assert (await manager.cancel(manager.store.rows[drafted["id"]]))["status"] == "cancelled"


async def test_a_draft_that_never_ran_is_not_cancelled(channels) -> None:
    manager = make_manager()
    drafted = await manager.draft("alex", spec())
    refused = await manager.cancel(manager.store.rows[drafted["id"]])
    assert refused["reason"] == "draft"
    assert f"delete_assignment({drafted['id']})" in refused["message"]


async def test_every_form_of_start_is_refused_while_busy(channels) -> None:
    manager = make_manager(delay=None)
    await manager.save("alex", "docs", spec())
    drafted = await manager.draft("alex", spec())
    running = await manager.start("alex", spec(), new=True)
    for refused in (
        await manager.start("alex", spec(), new=True),
        await manager.start("alex", saved="docs"),
        await manager.start("alex", draft=manager.store.rows[drafted["id"]]),
    ):
        assert refused["reason"] == "busy" and refused["running"]["id"] == running["id"]
    assert manager.store.rows[drafted["id"]]["status"] == "draft"
    assert sorted(manager.store.rows) == [drafted["id"], running["id"]]


async def test_an_answer_while_busy_is_refused_and_not_stored(channels) -> None:
    manager = make_manager(delay=None)
    parked = await manager.start("alex", spec())
    await manager.runner.cancel({"id": parked["id"]})
    await manager.store.update(parked["id"], status="waiting", open_question="Which?")
    running = await manager.start("alex", spec())
    refused = await manager.answer(manager.store.rows[parked["id"]], "ops")
    assert refused["reason"] == "busy" and refused["running"]["id"] == running["id"]
    assert refused["hint"] == (
        f"Assignment {running['id']} is still running; give the answer to assignment "
        f"{parked['id']} again when it ends."
    )
    row = manager.store.rows[parked["id"]]
    assert row["status"] == "waiting" and row["answer"] is None
    assert row["open_question"] == "Which?"


async def test_an_extension_while_busy_is_refused_and_changes_nothing(channels) -> None:
    manager = make_manager(delay=None)
    ended = await manager.start("alex", spec(budget={"tokens": 10}))
    await manager.runner.cancel({"id": ended["id"]})
    await manager.store.update(ended["id"], status="budget_exceeded", finished_at=FIXED_NOW)
    running = await manager.start("alex", spec())
    refused = await manager.extend(manager.store.rows[ended["id"]], tokens=50)
    assert refused["reason"] == "busy" and refused["running"]["id"] == running["id"]
    row = manager.store.rows[ended["id"]]
    assert row["status"] == "budget_exceeded" and row["spec"]["budget"]["tokens"] == 10
    await manager.cancel(manager.store.rows[running["id"]])
    extended = await manager.extend(manager.store.rows[ended["id"]], tokens=50)
    assert extended["status"] == "running"
    assert manager.store.rows[ended["id"]]["finished_at"] is None


async def test_a_restart_resumes_only_the_running_rows(channels) -> None:
    store = FakeStore()
    first = make_manager(delay=None, store=store)
    drafted = await first.draft("alex", spec())
    running = await first.start("alex", spec(), new=True)
    second = make_manager(delay=None, store=store)
    seen: list[int] = []

    async def spy(row, text, *, reason="answer"):
        seen.append(row["id"])

    second.runner.resume = spy  # type: ignore[method-assign]
    await second.open()
    assert seen == [running["id"]]
    assert store.rows[drafted["id"]]["status"] == "draft"


async def test_the_comments_of_a_draft_say_to_update_it(channels) -> None:
    manager = make_manager()
    drafted = await manager.draft("alex", spec())
    row = manager.store.rows[drafted["id"]]
    lines = definition.lines(row["spec"])
    title_line = next(n for n, text in enumerate(lines, 1) if text.startswith("title:"))
    await manager.store.add_comment(
        "alex",
        Parent("assignment", drafted["id"]),
        target="definition",
        line_no=title_line,
        line_text=lines[title_line - 1],
        body="A shorter title.",
    )
    got = await manager.open_comments("alex", row=row)
    assert f"update_assignment({drafted['id']}, spec)" in got["message"]
    assert [c["stale"] for c in got["comments"]] == [False]
    updated = await manager.update_draft(row, spec(title="Audit"))
    assert (updated["open_comments"], updated["stale_comments"]) == (1, 1)
    assert "1 open comment is stale" in updated["message"]
    after = await manager.open_comments("alex", row=manager.store.rows[drafted["id"]])
    assert [c["stale"] for c in after["comments"]] == [True]


# --- over MCP and the read API ----------------------------------------------


async def test_the_draft_tools_over_mcp(app, store) -> None:
    async with mcp_session(app) as session:
        drafted = payload(await session.call_tool("draft_assignment", {"spec": spec()}))
        draft_id = drafted["id"]
        updated = payload(
            await session.call_tool(
                "update_assignment", {"id": draft_id, "spec": spec(title="Docs audit")}
            )
        )
        listed = payload(await session.call_tool("list_assignments", {"state": "draft"}))
        both = payload(
            await session.call_tool("start_assignment", {"id": draft_id, "spec": spec()})
        )
        started = payload(await session.call_tool("start_assignment", {"id": draft_id}))
        again = payload(
            await session.call_tool("update_assignment", {"id": draft_id, "spec": spec()})
        )
        deleted = payload(await session.call_tool("delete_assignment", {"id": draft_id}))
    assert drafted["status"] == "draft"
    assert updated["title"] == "Docs audit"
    assert [a["id"] for a in listed["assignments"]] == [draft_id]
    assert both["reason"] == "invalid_arguments"
    assert started["id"] == draft_id and started["status"] == "running"
    assert again["reason"] == "not_a_draft"
    assert deleted["reason"] == "not_a_draft"
    assert store.rows[draft_id]["spec"]["title"] == "Docs audit"


async def test_a_busy_start_over_mcp_names_the_running_id(holding_app, store) -> None:
    async with mcp_session(holding_app) as session:
        first = payload(await session.call_tool("start_assignment", {"spec": spec()}))
        refused = payload(await session.call_tool("start_assignment", {"spec": spec()}))
    assert refused["status"] == "rejected" and refused["reason"] == "busy"
    assert refused["running"] == {"id": first["id"], "title": "Audit the gitops repo"}
    assert refused["hint"].startswith(f"Assignment {first['id']} is still running")
    assert list(store.rows) == [first["id"]]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("start_assignment", {}),
        ("update_assignment", {"spec": spec()}),
        ("delete_assignment", {}),
    ],
)
async def test_another_person_cannot_touch_a_draft(app, store, tool, args) -> None:
    async with mcp_sessions(app, ALEX, MIA) as (alex, mia):
        drafted = payload(await alex.call_tool("draft_assignment", {"spec": spec()}))
        result = await mia.call_tool(tool, {"id": drafted["id"], **args})
    assert error(result).endswith(f"404 not found: no assignment {drafted['id']}")
    row = store.rows[drafted["id"]]
    assert row["status"] == "draft" and row["spec"]["title"] == "Audit the gitops repo"


async def test_a_draft_takes_comments_through_the_read_api(app, store) -> None:
    drafted = await get_manager().draft("alex", spec())
    lines = definition.lines(store.rows[drafted["id"]]["spec"])
    base = f"/v1/assignments/{drafted['id']}"
    created = await post(app, base, 1, lines[0], "Say which repo.")
    assert created.status_code == 201
    async with mcp_session(app) as session:
        got = payload(await session.call_tool("assignment_comments", {"id": drafted["id"]}))
    assert [c["body"] for c in got["comments"]] == ["Say which repo."]
    assert "update_assignment" in got["message"]
