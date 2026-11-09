"""The review loop: a re-run keeps its lineage, a resolution names what
changed, and a refusal writes one log line with no argument in it."""

from __future__ import annotations

import logging

import pytest
from conftest import ALEX, MIA, error, mcp_session, payload, spec
from joshua_assignments import manager as manager_mod
from joshua_assignments import prompts
from joshua_assignments.comments import CommentRefused, NothingApplied, applied_ref, applied_refs
from joshua_assignments.server import get_manager
from test_manager import ended, make_manager
from test_read_api import call

NOTE = "The plan document changed: keep the redirects."


# --- start_assignment(rerun=N) -----------------------------------------------


async def test_a_rerun_is_a_new_run_with_the_spec_and_the_parent(channels) -> None:
    manager = make_manager()
    first = await ended(manager)
    again = await manager.start("alex", rerun=first["id"], note=f"  {NOTE} ")
    assert again["status"] == "running" and again["id"] != first["id"]
    assert again["parent_id"] == first["id"]
    assert again["rerun_of"] == {"id": first["id"], "title": first["title"]}
    assert f"re-run of assignment {first['id']}" in again["message"]
    row = manager.store.rows[again["id"]]
    assert row["parent_id"] == first["id"]
    assert row["spec"]["rerun_note"] == NOTE
    assert row["spec"]["goal"] == (
        f"{manager_mod.RERUN_NOTE_LEAD}{NOTE}\n\n{manager.store.rows[first['id']]['spec']['goal']}"
    )
    assert row["spec"]["title"] == manager.store.rows[first["id"]]["spec"]["title"]


async def test_a_rerun_of_a_rerun_carries_only_the_new_note(channels) -> None:
    manager = make_manager()
    first = await ended(manager)
    second = await manager.start("alex", rerun=first["id"], note="First note.")
    await manager.runner.idle()
    third = await manager.start("alex", rerun=second["id"], note="Second note.")
    goal = manager.store.rows[third["id"]]["spec"]["goal"]
    assert goal.startswith(f"{manager_mod.RERUN_NOTE_LEAD}Second note.\n\n")
    assert "First note." not in goal
    await manager.runner.idle()
    plain = await manager.start("alex", rerun=third["id"])
    row = manager.store.rows[plain["id"]]
    assert "rerun_note" not in row["spec"]
    assert row["spec"]["goal"] == manager.store.rows[first["id"]]["spec"]["goal"]


async def test_a_rerun_keeps_the_saved_assignment(channels) -> None:
    manager = make_manager()
    await manager.save("alex", "docs", spec())
    first = await manager.start("alex", saved="docs")
    await manager.runner.idle()
    again = await manager.start("alex", rerun=first["id"])
    assert again["saved"] == first["saved"]


async def test_a_rerun_of_a_draft_is_refused(channels) -> None:
    manager = make_manager()
    drafted = await manager.draft("alex", spec())
    refused = await manager.start("alex", rerun=drafted["id"], note="x")
    assert refused["reason"] == "is_a_draft"
    assert f"start_assignment(id={drafted['id']})" in refused["hint"]
    assert list(manager.store.rows) == [drafted["id"]]


async def test_a_rerun_of_another_persons_run_is_not_found(channels) -> None:
    manager = make_manager()
    theirs = await ended(manager, "mia")
    refused = await manager.start("alex", rerun=theirs["id"])
    assert refused["reason"] == "not_found" and refused["hint"]
    missing = await manager.start("alex", rerun=999)
    assert missing["reason"] == "not_found"
    # The same words: a refusal does not tell whether the row exists.
    assert refused["message"].replace(str(theirs["id"]), "N") == missing["message"].replace(
        "999", "N"
    )
    assert len(manager.store.rows) == 1


async def test_a_rerun_of_a_run_in_progress_is_refused(channels) -> None:
    manager = make_manager(delay=None)
    running = await manager.start("alex", spec())
    refused = await manager.start("alex", rerun=running["id"])
    assert refused["reason"] == "not_ended" and refused["hint"]


@pytest.mark.parametrize(
    "args",
    [
        {"note": "x"},
        {"rerun": 1, "saved": "docs"},
        {"rerun": 1, "raw_spec": spec()},
        {"rerun": "1"},
        {"rerun": 1, "note": "x" * (manager_mod.MAX_RERUN_NOTE + 1)},
        {"rerun": 1, "note": 5},
    ],
)
async def test_bad_rerun_arguments_are_refused(channels, args) -> None:
    manager = make_manager()
    await ended(manager)
    refused = await manager.start("alex", **args)
    assert refused["reason"] == "invalid_arguments" and refused["hint"]
    assert len(manager.store.rows) == 1


async def test_a_rerun_while_busy_is_refused_and_stores_nothing(channels) -> None:
    manager = make_manager(delay=None)
    first = await ended(manager)
    await manager.start("alex", spec(), new=True)
    refused = await manager.start("alex", rerun=first["id"])
    assert refused["reason"] == "busy"
    assert len(manager.store.rows) == 2


async def test_a_rerun_is_not_drafts_open(channels) -> None:
    manager = make_manager()
    first = await ended(manager)
    await manager.draft("alex", spec(title="Other"))
    assert (await manager.start("alex", rerun=first["id"]))["status"] == "running"


async def test_status_gives_parent_id_and_rerun_of(channels) -> None:
    manager = make_manager()
    first = await ended(manager)
    again = await manager.start("alex", rerun=first["id"], note=NOTE)
    got = await manager.status(manager.store.rows[again["id"]])
    assert got["parent_id"] == first["id"]
    assert got["rerun_of"] == {"id": first["id"], "title": first["title"]}
    assert got["rerun_note"] == NOTE
    root = await manager.status(manager.store.rows[first["id"]])
    assert root["parent_id"] is None and root["rerun_of"] is None


async def test_the_tool_takes_rerun_and_note(app, store) -> None:
    async with mcp_session(app) as session:
        started = payload(await session.call_tool("start_assignment", {"spec": spec()}))
        await get_manager().runner.idle()
        again = payload(
            await session.call_tool(
                "start_assignment", {"rerun": started["id"], "note": "Read the plan again."}
            )
        )
        both = payload(
            await session.call_tool("start_assignment", {"id": started["id"], "rerun": 1})
        )
        status = payload(await session.call_tool("assignment_status", {"id": again["id"]}))
    assert again["parent_id"] == started["id"]
    assert both["reason"] == "invalid_arguments"
    assert status["rerun_of"]["id"] == started["id"]


async def test_the_read_api_gives_the_lineage_of_the_person(app, store) -> None:
    manager = get_manager()
    first = await ended(manager)
    second = await manager.start("alex", rerun=first["id"])
    await manager.runner.idle()
    third = await manager.start("alex", rerun=second["id"])
    await manager.runner.idle()
    body = (await call(app, f"/v1/assignments/{second['id']}", person="alex")).json()
    assert body["parent_id"] == first["id"]
    assert body["rerun_of"] == {"id": first["id"], "title": first["title"]}
    assert [run["id"] for run in body["lineage"]] == [first["id"], second["id"], third["id"]]
    assert [run["parent_id"] for run in body["lineage"]] == [None, first["id"], second["id"]]
    root = (await call(app, f"/v1/assignments/{first['id']}", person="alex")).json()
    assert root["rerun_of"] is None and len(root["lineage"]) == 3
    items = (await call(app, "/v1/assignments", person="alex")).json()["items"]
    assert {i["id"]: i["parent_id"] for i in items}[third["id"]] == second["id"]
    assert (await call(app, f"/v1/assignments/{second['id']}", person="mia")).status_code == 404


async def test_the_lineage_leaves_out_the_rows_of_another_person(app, store) -> None:
    manager = get_manager()
    first = await ended(manager)
    # A row of another person can not be a re-run of this one; the read API
    # filters it all the same.
    other = await store.create(
        person_id="mia",
        conversation_id=None,
        spec=spec(),
        model="m",
        status="done",
        parent_id=first["id"],
    )
    body = (await call(app, f"/v1/assignments/{first['id']}", person="alex")).json()
    assert [run["id"] for run in body["lineage"]] == [first["id"]]
    assert other["id"] not in [run["id"] for run in body["lineage"]]


def test_the_first_message_of_a_rerun_names_the_parent() -> None:
    row = {"id": 9, "parent_id": 4, "person_id": "alex", "spec": {"goal": "Tidy up."}}
    text = prompts.first_prompt(row)
    assert text.startswith(
        "This is a re-run of assignment 4; the person has changed the plan document since."
    )
    assert "re-run" not in prompts.first_prompt({**row, "parent_id": None})


# --- resolve_comment(applied) ------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "ref"),
    [
        ("documents/alex/plan.md", "documents/alex/plan.md"),
        ("/documents/shared/docs-plan.v2.md", "documents/shared/docs-plan.v2.md"),
        ("documents/alex/board.html", "documents/alex/board.html"),
        ("task 78", "task 78"),
        ("Task #078", "task 78"),
        ("assignment 6", "assignment 6"),
        ("saved Docs-Cleanup", "saved docs-cleanup"),
        (
            "https://example.com/group/project/-/issues/3",
            "https://example.com/group/project/-/issues/3",
        ),
    ],
)
def test_a_reference_of_applied(raw, ref) -> None:
    assert applied_ref(raw) == ref


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "the plan",
        "documents/alex/../bob/plan.md",
        "documents/Bad Owner/plan.md",
        "documents/alex/plan.txt",
        "documents/alex/sub/plan.md",
        "task",
        "task seventy",
        "javascript:alert(1)",
        "ftp://example.com/x",
        "https://",
        "https://example.com/a b",
        "saved X",
        "x" * 501,
        5,
    ],
)
def test_a_bad_reference_of_applied(raw) -> None:
    assert applied_ref(raw) is None


def test_applied_is_a_list_with_at_least_one_reference() -> None:
    for empty in (None, []):
        with pytest.raises(NothingApplied):
            applied_refs(empty)
    with pytest.raises(CommentRefused):
        applied_refs("task 1")
    with pytest.raises(CommentRefused):
        applied_refs(["task 1", "the plan"])
    with pytest.raises(CommentRefused):
        applied_refs([f"task {n}" for n in range(21)])
    assert applied_refs(["task 1", "Task #1", "documents/alex/a.md"]) == [
        "task 1",
        "documents/alex/a.md",
    ]


async def _report_comment(store) -> int:
    row = await store.create(
        person_id="alex",
        conversation_id=None,
        spec=spec(),
        model="m",
        status="done",
    )
    store.rows[row["id"]]["report"] = "# Done\n\nIt is done.\n"
    from joshua_assignments.store import Parent

    made = await store.add_comment(
        "alex",
        Parent("assignment", row["id"]),
        target="report",
        line_no=3,
        line_text="It is done.",
        body="Keep the redirects.",
    )
    return int(made["id"])


async def test_a_resolution_with_nothing_applied_is_refused(app, store) -> None:
    cid = await _report_comment(store)
    async with mcp_session(app) as session:
        none = payload(
            await session.call_tool("resolve_comment", {"comment_id": cid, "resolution": "Done."})
        )
        empty = payload(
            await session.call_tool(
                "resolve_comment", {"comment_id": cid, "resolution": "Done.", "applied": []}
            )
        )
        bad = payload(
            await session.call_tool(
                "resolve_comment",
                {"comment_id": cid, "resolution": "Done.", "applied": ["the plan"]},
            )
        )
    assert none["reason"] == empty["reason"] == "nothing_applied"
    assert none["hint"]
    assert bad["reason"] == "invalid_arguments"
    assert store.comment_rows[cid]["resolved_at"] is None


async def test_a_resolution_stores_applied_and_the_read_api_gives_it(app, store) -> None:
    cid = await _report_comment(store)
    async with mcp_session(app) as session:
        done = payload(
            await session.call_tool(
                "resolve_comment",
                {
                    "comment_id": cid,
                    "resolution": "Kept the redirects in the plan.",
                    "applied": ["documents/alex/docs-plan.md", "task #78"],
                },
            )
        )
    assert done["applied"] == ["documents/alex/docs-plan.md", "task 78"]
    assert store.comment_rows[cid]["applied"] == ["documents/alex/docs-plan.md", "task 78"]
    row_id = store.comment_rows[cid]["assignment_id"]
    [got] = (await call(app, f"/v1/assignments/{row_id}/comments", person="alex")).json()["items"]
    assert got["applied"] == ["documents/alex/docs-plan.md", "task 78"]
    assert got["resolution"] == "Kept the redirects in the plan."


async def test_another_person_cannot_resolve_with_applied(app, store) -> None:
    cid = await _report_comment(store)
    async with mcp_session(app, token=MIA) as session:
        got = payload(
            await session.call_tool(
                "resolve_comment",
                {"comment_id": cid, "resolution": "x", "applied": ["task 1"]},
            )
        )
    assert got["reason"] == "not_found"
    assert store.comment_rows[cid]["resolved_at"] is None


# --- the log of a refusal ----------------------------------------------------


def _refusals(caplog) -> list[dict]:
    return [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("message") == "tool refused"
    ]


async def test_a_refusal_writes_one_line_with_no_argument(holding_app, store, caplog) -> None:
    cid = await _report_comment(store)
    secret_goal = "Read the private plan of the person."
    with caplog.at_level(logging.INFO):
        async with mcp_session(holding_app, token=ALEX) as session:
            await session.call_tool(
                "resolve_comment", {"comment_id": cid, "resolution": "Secret resolution."}
            )
            await session.call_tool("start_assignment", {"spec": {"goal": secret_goal}})
            await session.call_tool("start_assignment", {"spec": {"goal": secret_goal}})
            missing = await session.call_tool("cancel_assignment", {"id": 999})
    assert error(missing).endswith("404 not found: no assignment 999")
    lines = _refusals(caplog)
    assert [(line["tool"], line["reason"]) for line in lines] == [
        ("resolve_comment", "nothing_applied"),
        ("start_assignment", "busy"),
    ]
    assert all(line["person"] == "alex" for line in lines)
    assert all(set(line) == {"message", "tool", "person", "reason"} for line in lines)
    assert secret_goal not in caplog.text
    assert "Secret resolution." not in caplog.text
    assert "Keep the redirects." not in caplog.text


async def test_a_reply_that_is_not_a_refusal_writes_no_refusal_line(app, caplog) -> None:
    with caplog.at_level(logging.INFO):
        async with mcp_session(app) as session:
            await session.call_tool("list_assignments", {})
    assert _refusals(caplog) == []
