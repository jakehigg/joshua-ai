"""The tools over MCP, and the negative set: no bearer, a wrong bearer, a caller
with no person, a guest, and another person's assignment."""

from __future__ import annotations

import httpx
import pytest
from conftest import ADDON, ALEX, MIA, SAM, error, mcp_session, mcp_sessions, payload, settle, spec
from joshua_assignments.server import find_caller, get_manager, mcp
from joshua_assignments.settings import Caller


async def post_mcp(app, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/mcp", headers=headers, json={})


async def get(app, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


async def test_a_missing_bearer_gets_401(app) -> None:
    assert (await post_mcp(app, {})).status_code == 401


async def test_a_wrong_bearer_gets_401(app) -> None:
    assert (await post_mcp(app, {"Authorization": "Bearer nope"})).status_code == 401


async def test_a_bearer_without_the_scheme_gets_401(app) -> None:
    assert (await post_mcp(app, {"Authorization": ALEX})).status_code == 401


async def test_an_unknown_path_needs_a_bearer_too(app) -> None:
    assert (await get(app, "/anything")).status_code == 401


async def test_healthz_is_open_and_names_nothing(app) -> None:
    response = await get(app, "/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_readyz_is_open_and_gives_booleans_and_counts_only(app, store) -> None:
    async with mcp_session(app) as session:
        payload(await session.call_tool("start_assignment", {"spec": spec()}))
    await settle()
    response = await get(app, "/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "ok": True,
        "database": True,
        "channels": True,
        "people": 3,
        "queued": 0,
        "running": 0,
        "waiting": 0,
        "paused_limits": 0,
    }
    for secret in ("alex", "mia", ALEX, MIA, ADDON, "Audit"):
        assert secret not in response.text


async def test_readyz_is_503_when_the_database_is_down(app, store) -> None:
    store.healthy = False
    response = await get(app, "/readyz")
    assert response.status_code == 503
    assert response.json()["database"] is False


def test_find_caller(settings) -> None:
    assert find_caller(settings, f"Bearer {ALEX}") == Caller(person="alex")
    assert find_caller(settings, f"Bearer {ADDON}") == Caller(person=None)
    assert find_caller(settings, "Bearer ") is None
    assert find_caller(settings, None) is None
    assert find_caller(settings, f"Basic {ALEX}") is None


async def test_a_person_starts_lists_reads_and_the_stub_finishes(app, channels) -> None:
    async with mcp_session(app) as session:
        started = payload(await session.call_tool("start_assignment", {"spec": spec()}))
        await settle()
        status = payload(await session.call_tool("assignment_status", {"id": started["id"]}))
        listed = payload(await session.call_tool("list_assignments", {}))
    assert started["id"] == 1
    assert "page_path" not in started
    assert started["title"] == "Audit the gitops repo"
    assert started["model"] == "sonnet"
    assert started["budget"] == {"five_hour_pct": 70, "seven_day_pct": 85, "tokens": None}
    assert status["status"] == "done"
    assert status["person"] == "alex"
    assert "stub runner" in status["summary"]
    assert status["last_checkpoint"]["note"] == "the stub runner finished"
    assert status["title"] == "Audit the gitops repo" and status["state"] == "completed"
    assert status["report"].startswith(
        "# Audit the gitops repo\n\nThis is a test report of the stub runner.\n\n## Highlights"
    )
    assert "## Cost" in status["report"] and status["report_at"] is not None
    assert status["plan"] is None and status["outputs"] == []
    assert [a["id"] for a in listed["assignments"]] == [1]
    assert listed["assignments"][0]["title"] == "Audit the gitops repo"
    assert listed["assignments"][0]["state"] == "completed"
    assert len(channels.requests) == 1
    assert channels.bodies[0]["destination"] == "telegram:dm:alex"


async def test_a_person_cancels_a_running_assignment(holding_app) -> None:
    async with mcp_session(holding_app) as session:
        started = payload(await session.call_tool("start_assignment", {"spec": spec()}))
        assert started["status"] == "running"
        cancelled = payload(await session.call_tool("cancel_assignment", {"id": started["id"]}))
        status = payload(await session.call_tool("assignment_status", {"id": started["id"]}))
    assert cancelled["status"] == "cancelled"
    assert status["status"] == "cancelled" and status["state"] == "completed"
    assert "assignment_status" in cancelled["message"] and "page" not in cancelled["message"]
    assert "page_path" not in cancelled
    assert status["report"].startswith(
        "# Audit the gitops repo\n\nGenerated by the runner at cancelled,"
    )
    assert "**Stopped at cancelled** It was stopped with cancel_assignment." in status["report"]


async def test_the_conversation_header_is_kept(app, store) -> None:
    async with mcp_session(app, headers={"X-Joshua-Conversation": "conv-1"}) as session:
        payload(await session.call_tool("start_assignment", {"spec": spec()}))
    assert store.rows[1]["conversation_id"] == "conv-1"


async def test_another_person_gets_404_for_an_assignment(holding_app) -> None:
    async with mcp_sessions(holding_app, ALEX, MIA) as (alex, mia):
        started = payload(await alex.call_tool("start_assignment", {"spec": spec()}))
        assignment_id = started["id"]
        results = [
            await mia.call_tool("assignment_status", {"id": assignment_id}),
            await mia.call_tool("cancel_assignment", {"id": assignment_id}),
            await mia.call_tool("answer_assignment", {"id": assignment_id, "text": "x"}),
            await mia.call_tool("extend_assignment", {"id": assignment_id, "tokens": 5}),
        ]
        listed = payload(await mia.call_tool("list_assignments", {}))
        still = payload(await alex.call_tool("assignment_status", {"id": assignment_id}))
    for result in results:
        assert error(result).endswith(f"404 not found: no assignment {assignment_id}")
    assert listed["assignments"] == []
    assert still["status"] == "running"


async def test_an_unknown_id_is_404(app) -> None:
    async with mcp_session(app) as session:
        result = await session.call_tool("assignment_status", {"id": 99})
    assert error(result).endswith("404 not found: no assignment 99")


PERSON_TOOLS = [
    ("start_assignment", {"spec": spec()}),
    ("answer_assignment", {"id": 1, "text": "x"}),
    ("cancel_assignment", {"id": 1}),
    ("extend_assignment", {"id": 1, "tokens": 5}),
    ("save_assignment", {"name": "docs-cleanup", "spec": spec()}),
    ("list_saved_assignments", {}),
    ("delete_saved_assignment", {"name": "docs-cleanup"}),
    ("assignment_comments", {"saved": "docs-cleanup"}),
    ("resolve_comment", {"comment_id": 1, "resolution": "Done."}),
]
READ_TOOLS = {"assignment_status", "list_assignments"}


@pytest.mark.parametrize(("tool", "args"), PERSON_TOOLS)
async def test_the_addon_token_cannot_use_a_person_tool(app, store, tool: str, args) -> None:
    async with mcp_session(app, token=ADDON) as session:
        result = await session.call_tool(tool, args)
    assert error(result).endswith("403 forbidden: this tool needs a person")
    assert store.rows == {}
    assert store.saved == {}


async def test_every_person_tool_is_in_the_negative_test() -> None:
    tools = {tool.name for tool in await mcp.list_tools()}
    assert tools - READ_TOOLS == {name for name, _ in PERSON_TOOLS}


async def test_the_addon_token_may_read_every_assignment(app) -> None:
    async with mcp_sessions(app, ALEX, ADDON) as (alex, addon):
        started = payload(await alex.call_tool("start_assignment", {"spec": spec()}))
        await settle()
        status = payload(await addon.call_tool("assignment_status", {"id": started["id"]}))
        listed = payload(await addon.call_tool("list_assignments", {}))
    assert status["person"] == "alex"
    assert [a["id"] for a in listed["assignments"]] == [started["id"]]


@pytest.mark.parametrize(
    ("tool", "args"),
    [*PERSON_TOOLS, ("assignment_status", {"id": 1}), ("list_assignments", {})],
)
async def test_a_guest_gets_403_on_every_tool(app, store, tool: str, args) -> None:
    async with mcp_session(app, token=SAM) as session:
        result = await session.call_tool(tool, args)
    assert error(result).endswith("403 forbidden: assignments are for members only")
    assert store.rows == {}


async def test_a_clamped_ceiling_is_in_the_reply(app, store) -> None:
    async with mcp_session(app) as session:
        started = payload(
            await session.call_tool(
                "start_assignment", {"spec": spec(budget={"five_hour_pct": 99})}
            )
        )
    assert started["budget"]["five_hour_pct"] == 90
    assert "budget.five_hour_pct 99 is above the maximum 90, so it is 90" in started["message"]
    assert started["notes"] == ["budget.five_hour_pct 99 is above the maximum 90, so it is 90"]
    assert store.rows[1]["spec"]["budget"]["five_hour_pct"] == 90


async def test_a_repo_outside_the_allowlist_is_refused(app, store) -> None:
    async with mcp_session(app) as session:
        result = payload(
            await session.call_tool(
                "start_assignment", {"spec": spec(repos=["github.com/example-org/app"])}
            )
        )
    assert result["status"] == "rejected"
    assert result["reason"] == "repo_not_allowed"
    assert store.rows == {}


async def test_a_model_not_in_models_is_refused(app, store) -> None:
    async with mcp_session(app) as session:
        result = payload(await session.call_tool("start_assignment", {"spec": spec(model="gpt")}))
    assert result["reason"] == "unknown_model"
    assert store.rows == {}


async def test_extend_and_answer_over_mcp(holding_app, store) -> None:
    async with mcp_session(holding_app) as session:
        started = payload(await session.call_tool("start_assignment", {"spec": spec()}))
        extended = payload(
            await session.call_tool(
                "extend_assignment", {"id": started["id"], "five_hour_pct": 95, "tokens": 10}
            )
        )
        store.rows[started["id"]]["status"] = "waiting"
        answered = payload(
            await session.call_tool("answer_assignment", {"id": started["id"], "text": "yes"})
        )
    assert extended["budget"]["five_hour_pct"] == 90
    assert extended["budget"]["tokens"] == 10
    assert answered["status"] == "running"
    assert store.rows[started["id"]]["answer"] == "yes"


async def test_the_lifespan_opens_and_closes_the_store(settings, store, cfg, channels) -> None:
    from conftest import build

    app = build(settings, store, cfg)
    inner = app.app
    async with inner.router.lifespan_context(inner):
        assert store.opened is True
        assert get_manager().store is store
    assert store.closed is True


def test_get_manager_before_build_app_fails() -> None:
    from joshua_assignments import server

    server._manager = None
    with pytest.raises(RuntimeError):
        get_manager()


async def test_saved_assignments_over_mcp(app, store) -> None:
    async with mcp_sessions(app, ALEX, MIA) as (alex, mia):
        saved = payload(
            await alex.call_tool(
                "save_assignment", {"name": "docs-cleanup", "spec": spec(title="Docs")}
            )
        )
        started = payload(
            await alex.call_tool(
                "start_assignment", {"saved": "docs-cleanup", "spec": {"model": "opus"}}
            )
        )
        await settle()
        status = payload(await alex.call_tool("assignment_status", {"id": started["id"]}))
        listed = payload(await alex.call_tool("list_assignments", {}))
        mine = payload(await alex.call_tool("list_saved_assignments", {}))
        theirs = payload(await mia.call_tool("list_saved_assignments", {}))
        refused = payload(await mia.call_tool("start_assignment", {"saved": "docs-cleanup"}))
        gone = {"name": "docs-cleanup"}
        not_theirs = payload(await mia.call_tool("delete_saved_assignment", gone))
        deleted = payload(await alex.call_tool("delete_saved_assignment", gone))
    ref = {"id": saved["id"], "name": "docs-cleanup"}
    assert started["saved"] == ref
    assert "saved assignment docs-cleanup" in started["message"]
    assert store.rows[started["id"]]["spec"]["model"] == "opus"
    assert status["saved"] == ref
    assert listed["assignments"][0]["saved"] == ref
    [one] = mine["saved"]
    assert (one["name"], one["title"], one["runs"]) == ("docs-cleanup", "Docs", 1)
    assert one["last_run"]["id"] == started["id"]
    assert theirs == {"saved": []}
    assert refused["reason"] == "not_found"
    assert not_theirs["reason"] == "not_found"
    assert deleted["status"] == "deleted"
    assert store.saved == {}


async def test_start_assignment_needs_a_spec_or_a_saved_name(app, store) -> None:
    async with mcp_session(app) as session:
        result = payload(await session.call_tool("start_assignment", {}))
    assert result["reason"] == "invalid_arguments"
    assert store.rows == {}
