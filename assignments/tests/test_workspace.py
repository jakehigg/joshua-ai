"""The workspace rule in the assignments container: the workspace comes from
the ``_meta`` of a ``start_assignment`` call, is kept with the run, goes out on
the report and the question, and a call from a workspace gets a link to the
viewer page of the assignment."""

from __future__ import annotations

import json

from conftest import MIA, mcp_session, mcp_sessions, payload, settle, spec
from joshua_assignments import notify
from joshua_assignments.manager import rerun_spec, stamp_workspace
from joshua_assignments.server import WORKSPACE_META_KEY, mcp

WS = "11111111-1111-4111-8111-111111111111"
META = {WORKSPACE_META_KEY: WS}


def with_meta(session, tool: str, args: dict, meta: dict | None = META):
    return session.call_tool(tool, args, meta=meta)


def text_and_links(result):
    texts = [b for b in result.content if b.type == "text"]
    links = [b for b in result.content if b.type == "resource_link"]
    assert result.is_error is not True
    return json.loads(texts[0].text), links


async def test_start_keeps_the_workspace_and_returns_a_link(app, store, channels) -> None:
    async with mcp_session(app) as session:
        result = await with_meta(session, "start_assignment", {"spec": spec()})
        reply, links = text_and_links(result)
        await settle()
    assert reply["id"] == 1
    assert result.structured_content["id"] == 1
    assert [(link.uri, link.name) for link in links] == [
        ("/assignments/1", "Assignment 1: Audit the gitops repo")
    ]
    assert links[0].mime_type == "text/html"
    assert store.rows[1]["spec"]["workspace"] == WS


async def test_the_link_uses_the_viewer_url_when_it_is_set(app, store, channels, monkeypatch):
    from joshua_assignments import server

    config = server.get_manager().load_config()
    patched = config.model_copy(
        update={
            "workspaces": config.workspaces.model_copy(
                update={"viewer_url": "https://joshua.example.org/"}
            )
        }
    )
    monkeypatch.setattr(server.get_manager(), "load_config", lambda: patched)
    async with mcp_session(app) as session:
        result = await with_meta(session, "start_assignment", {"spec": spec()})
        _, links = text_and_links(result)
        await settle()
    assert links[0].uri == "https://joshua.example.org/assignments/1"


async def test_a_start_outside_a_workspace_has_no_workspace_and_no_link(
    app, store, channels
) -> None:
    async with mcp_session(app) as session:
        result = await session.call_tool("start_assignment", {"spec": spec()})
        started = payload(result)  # one text block: no link
        await settle()
    assert started["id"] == 1
    assert "workspace" not in store.rows[1]["spec"]
    assert "workspace" not in channels.bodies[0]
    assert channels.bodies[0]["destination"] == "telegram:dm:alex"


async def test_the_report_goes_out_with_the_workspace(app, store, channels) -> None:
    async with mcp_session(app) as session:
        await with_meta(session, "start_assignment", {"spec": spec()})
        await settle()
    assert len(channels.bodies) == 1
    assert channels.bodies[0]["workspace"] == WS
    assert "has ended" in channels.bodies[0]["text"]


async def test_the_question_goes_out_with_the_workspace(holding_app, store, channels) -> None:
    from joshua_assignments.server import get_manager

    async with mcp_session(holding_app) as session:
        started = text_and_links(await with_meta(session, "start_assignment", {"spec": spec()}))[0]
        row = store.rows[started["id"]]
        row["status"] = "waiting"
        row["open_question"] = "Which region?"
        await get_manager().stopped(started["id"])
    assert channels.bodies[-1]["workspace"] == WS
    assert "has a question" in channels.bodies[-1]["text"]


async def test_a_person_with_no_destination_still_gets_the_workspace_event(
    app, store, channels
) -> None:
    async with mcp_sessions(app, MIA) as (mia,):
        await with_meta(mia, "start_assignment", {"spec": spec()})
        await settle()
    assert channels.bodies[0]["workspace"] == WS
    assert "destination" not in channels.bodies[0]


async def test_a_spec_cannot_set_the_workspace(app, store) -> None:
    async with mcp_session(app) as session:
        result = await session.call_tool("start_assignment", {"spec": spec(workspace=WS)})
    assert json.loads(result.content[0].text)["reason"] == "invalid_spec"
    assert store.rows == {}


async def test_a_value_that_is_not_a_uuid_is_no_workspace(app, store, channels) -> None:
    async with mcp_session(app) as session:
        result = await with_meta(
            session, "start_assignment", {"spec": spec()}, {WORKSPACE_META_KEY: "../x"}
        )
        payload(result)  # no link: not a workspace call
        await settle()
    assert "workspace" not in store.rows[1]["spec"]


async def test_a_draft_takes_the_workspace_of_its_start(app, store, channels) -> None:
    async with mcp_session(app) as session:
        draft = payload(await session.call_tool("draft_assignment", {"spec": spec()}))
        assert "workspace" not in store.rows[draft["id"]]["spec"]
        started = await with_meta(session, "start_assignment", {"id": draft["id"]})
        reply, links = text_and_links(started)
        await settle()
    assert reply["id"] == draft["id"]
    assert links[0].uri == f"/assignments/{draft['id']}"
    assert store.rows[draft["id"]]["spec"]["workspace"] == WS
    assert channels.bodies[-1]["workspace"] == WS


async def test_status_from_a_workspace_returns_a_link_and_shows_the_workspace(
    app, store, channels
) -> None:
    async with mcp_session(app) as session:
        await with_meta(session, "start_assignment", {"spec": spec()})
        await settle()
        status = await with_meta(session, "assignment_status", {"id": 1})
        plain = await session.call_tool("assignment_status", {"id": 1})
    body, links = text_and_links(status)
    assert body["workspace"] == WS
    assert [link.uri for link in links] == ["/assignments/1"]
    assert payload(plain)["workspace"] == WS  # one block, no link


async def test_a_refusal_has_no_link(app, store, channels) -> None:
    async with mcp_session(app) as session:
        result = await with_meta(session, "start_assignment", {"spec": spec(model="nope")})
    assert not [b for b in result.content if b.type == "resource_link"]
    assert json.loads(result.content[0].text)["status"] == "rejected"


async def test_a_rerun_takes_the_workspace_of_its_own_start(app, store, channels) -> None:
    async with mcp_session(app) as session:
        await with_meta(session, "start_assignment", {"spec": spec()})
        await settle()
        again = await session.call_tool("start_assignment", {"rerun": 1})
        payload(again)
        await settle()
    assert "workspace" not in store.rows[2]["spec"]


def test_rerun_spec_and_stamp() -> None:
    assert "workspace" not in rerun_spec({"goal": "g", "workspace": WS}, None)
    out: dict = {"goal": "g", "workspace": "old"}
    stamp_workspace(out, None)
    assert out == {"goal": "g"}
    stamp_workspace(out, WS)
    assert out["workspace"] == WS


def test_send_event_body_names_the_workspace_and_the_destination_only_when_set() -> None:
    assert notify.workspace_of({"spec": {"workspace": WS}}) == WS
    assert notify.workspace_of({"spec": {}}) is None
    assert notify.workspace_of({}) is None


def test_the_tool_names_are_unchanged() -> None:
    # The gateway map keys on this name.
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "start_assignment" in names
