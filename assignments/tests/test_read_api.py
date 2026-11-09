"""The read API for the viewer: each route, each auth outcome, the person scope,
the state filter, the order, and the caps."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from conftest import ADDON, ALEX, LAPTOP, VIEWER, build, make_config, spec
from fakes import FIXED_NOW
from joshua_assignments import read_api
from joshua_assignments.server import get_manager

ITEM_KEYS = {
    "id",
    "title",
    "status",
    "state",
    "model",
    "created_at",
    "started_at",
    "finished_at",
    "working_s",
    "turns",
    "usage",
    "outputs_count",
    "lede",
    "next_step",
    "open_question",
    "open_request",
    "asked_at",
    "stopped_at_limits",
    "saved",
}
DETAIL_KEYS = ITEM_KEYS | {
    "spec",
    "plan",
    "plan_at",
    "report",
    "report_at",
    "outputs",
    "checkpoints",
    "summary",
    "follow_ups",
    "answer",
    "error",
    "definition",
    "comments",
    "open_comments",
    "granted_repos",
    "inbox",
}
SAVED_KEYS = {
    "id",
    "name",
    "title",
    "spec",
    "created_at",
    "updated_at",
    "runs",
    "last_run",
    "open_comments",
}


async def call(app, path: str, token: str | None = VIEWER, **params) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path, params=params, headers=headers)


async def make(store, person: str = "alex", status: str = "draft", **fields) -> dict:
    row = await store.create(
        person_id=person,
        conversation_id=None,
        spec={"goal": "Clean up the docs.", "model": "sonnet"},
        model="claude-sonnet-5",
    )
    store.rows[row["id"]].update(status=status, **fields)
    return store.rows[row["id"]]


async def ended(manager, *args, **kwargs) -> dict:
    """Start an assignment and end it at once, so the slot is free for the next one."""
    started = await manager.start(*args, **kwargs)
    await manager.runner.cancel({"id": started["id"]})
    manager.store.rows[started["id"]]["status"] = "done"
    return started


PATHS = ["/v1/assignments", "/v1/assignments/1", "/v1/saved", "/v1/saved/1"]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("token", [None, "nope", ALEX, ADDON, LAPTOP])
async def test_a_bearer_that_is_not_a_read_caller_gets_401(app, store, path, token) -> None:
    await make(store)
    response = await call(app, path, token=token, person="alex")
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


async def test_the_viewer_token_is_not_a_bearer_of_mcp(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/mcp", headers={"Authorization": f"Bearer {VIEWER}"}, json={})
    assert response.status_code == 401


async def test_read_callers_comes_from_the_config(settings, store, channels) -> None:
    cfg = make_config("  read_callers:\n    - laptop\n")
    app = build(settings, store, cfg)
    await get_manager().open()
    assert (await call(app, "/v1/saved", token=LAPTOP, person="alex")).status_code == 200
    assert (await call(app, "/v1/saved", token=VIEWER, person="alex")).status_code == 401


async def test_an_empty_read_callers_turns_the_api_off(settings, store, channels) -> None:
    app = build(settings, store, make_config("  read_callers:\n"))
    await get_manager().open()
    assert (await call(app, "/v1/saved", person="alex")).status_code == 401


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("person", [None, "", "Alex", "../alex", "a" * 33])
async def test_a_missing_or_bad_person_is_400(app, store, path, person) -> None:
    await make(store)
    params = {} if person is None else {"person": person}
    response = await call(app, path, **params)
    assert response.status_code == 400
    assert "person" in response.json()["error"]


async def test_the_list_gives_the_items_of_the_person_newest_first(app, store) -> None:
    first = await make(store, status="done", finished_at=FIXED_NOW, input=10, output=5)
    await make(store, person="mia")
    second = await make(
        store,
        status="waiting",
        started_at=FIXED_NOW,
        open_question="Which branch?",
        asked_at=FIXED_NOW,
        cache_read=2,
        cache_write=3,
        working_s=90,
        turns=7,
        plan="# Plan\n- [x] read\n- [ ] check\n- [ ] **Next:** fix the links",
    )
    await store.add_output(second["id"], "page", "docs/a.md", "A")
    response = await call(app, "/v1/assignments", person="alex")
    assert response.status_code == 200
    items = response.json()["items"]
    assert [i["id"] for i in items] == [second["id"], first["id"]]
    assert all(set(i) == ITEM_KEYS for i in items)
    newest, oldest = items
    assert newest["state"] == "in_progress" and newest["status"] == "waiting"
    assert newest["open_question"] == "Which branch?"
    assert newest["asked_at"] == FIXED_NOW.isoformat()
    assert newest["working_s"] == 90
    assert newest["turns"] == 7
    assert newest["outputs_count"] == 1
    assert newest["next_step"] == "fix the links"
    assert newest["lede"] is None
    assert newest["usage"] == {
        "input": 0,
        "output": 0,
        "cache_read": 2,
        "cache_write": 3,
        "total": 5,
    }
    assert newest["title"] == "Clean up the docs"
    assert newest["model"] == "sonnet"
    assert newest["saved"] is None
    assert oldest["state"] == "completed"
    assert oldest["usage"]["total"] == 15
    assert (oldest["outputs_count"], oldest["next_step"], oldest["lede"]) == (0, None, None)
    assert (await call(app, "/v1/assignments", person="sam")).json() == {"items": []}


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("draft", ["draft"]),
        ("in_progress", ["paused_limits", "running"]),
        ("completed", ["failed"]),
    ],
)
async def test_the_state_filter(app, store, state, expected) -> None:
    for status in ("running", "draft", "failed", "paused_limits"):
        await make(store, status=status)
    response = await call(app, "/v1/assignments", person="alex", state=state)
    assert [i["status"] for i in response.json()["items"]] == expected


@pytest.mark.parametrize("params", [{"state": "done"}, {"limit": "many"}])
async def test_a_bad_state_or_limit_is_400(app, params) -> None:
    response = await call(app, "/v1/assignments", person="alex", **params)
    assert response.status_code == 400


@pytest.mark.parametrize(("limit", "count"), [("1", 1), ("0", 1), ("500", 205), (None, 50)])
async def test_the_limit_is_capped(app, store, monkeypatch, limit, count) -> None:
    for _ in range(205):
        await make(store)
    monkeypatch.setattr(read_api, "MAX_LIMIT", 205)
    params = {"person": "alex"} | ({"limit": limit} if limit is not None else {})
    items = (await call(app, "/v1/assignments", **params)).json()["items"]
    assert len(items) == count


def test_the_caps_are_the_contract() -> None:
    assert (read_api.MAX_LIMIT, read_api.MAX_CHECKPOINTS, read_api.MAX_RUNS) == (200, 200, 50)


async def test_the_detail_of_an_assignment(app, store) -> None:
    row = await make(
        store,
        status="done",
        plan="# Plan\n- [x] read",
        plan_at=FIXED_NOW,
        report="# Docs\nThe docs have no broken links now.\n\n## Highlights",
        report_at=FIXED_NOW,
        summary="Done.",
        follow_ups=[{"title": "More"}],
        answer="main",
        stopped_at_limits={"window": "five_hour", "used_pct": 80},
    )
    await store.add_output(row["id"], "page", "https://docs.example.com/a", "A page")
    for n in range(3):
        await store.add_checkpoint(row["id"], f"step {n}")
    response = await call(app, f"/v1/assignments/{row['id']}", person="alex")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == DETAIL_KEYS
    assert body["spec"] == {"goal": "Clean up the docs.", "model": "sonnet"}
    assert body["plan"] == "# Plan\n- [x] read" and body["plan_at"] == FIXED_NOW.isoformat()
    assert body["report"].startswith("# Docs") and body["report_at"] == FIXED_NOW.isoformat()
    assert body["lede"] == "The docs have no broken links now."
    assert body["next_step"] is None and body["outputs_count"] == 1
    [output] = body["outputs"]
    assert set(output) == {"kind", "ref", "title", "at"}
    assert [c["note"] for c in body["checkpoints"]] == ["step 2", "step 1", "step 0"]
    assert body["checkpoints"][0]["at"] == FIXED_NOW.isoformat()
    assert (body["summary"], body["answer"], body["error"]) == ("Done.", "main", None)
    assert body["follow_ups"] == [{"title": "More"}]
    assert body["stopped_at_limits"] == {"window": "five_hour", "used_pct": 80}


async def test_the_checkpoints_are_capped(app, store, monkeypatch) -> None:
    row = await make(store)
    for n in range(5):
        await store.add_checkpoint(row["id"], f"step {n}")
    monkeypatch.setattr(read_api, "MAX_CHECKPOINTS", 2)
    body = (await call(app, f"/v1/assignments/{row['id']}", person="alex")).json()
    assert [c["note"] for c in body["checkpoints"]] == ["step 4", "step 3"]


async def test_another_persons_assignment_is_404(app, store) -> None:
    row = await make(store, person="mia")
    theirs = await call(app, f"/v1/assignments/{row['id']}", person="alex")
    missing = await call(app, "/v1/assignments/999", person="alex")
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json() == missing.json() == {"error": "not found"}
    assert (await call(app, f"/v1/assignments/{row['id']}", person="mia")).status_code == 200


async def test_the_saved_list_and_detail(app, store) -> None:
    manager = get_manager()
    other = await manager.save("alex", "never-run", spec(title="Never"))
    saved = await manager.save("alex", "docs", spec(title="Docs"))
    await manager.save("mia", "docs", spec())
    runs = [await ended(manager, "alex", saved="docs") for _ in range(3)]
    await manager.draft("alex", saved="docs")
    await ended(manager, "alex", spec())
    listed = (await call(app, "/v1/saved", person="alex")).json()["items"]
    assert [s["name"] for s in listed] == ["docs", "never-run"]
    assert all(set(s) == SAVED_KEYS for s in listed)
    docs, never = listed
    assert docs["runs"] == 3 and docs["title"] == "Docs"
    assert docs["last_run"]["id"] == runs[-1]["id"]
    assert set(docs["last_run"]) == {"id", "status", "finished_at"}
    assert docs["spec"]["title"] == "Docs"
    assert never["runs"] == 0 and never["last_run"] is None
    assert never["id"] == other["id"]

    body = (await call(app, f"/v1/saved/{saved['id']}", person="alex")).json()
    assert set(body) == SAVED_KEYS | {"runs_list", "definition", "comments"}
    assert [r["id"] for r in body["runs_list"]] == [r["id"] for r in reversed(runs)]
    assert all(set(r) == ITEM_KEYS for r in body["runs_list"])
    assert body["runs_list"][0]["saved"] == {"id": saved["id"], "name": "docs"}
    item = (await call(app, f"/v1/assignments/{runs[0]['id']}", person="alex")).json()
    assert item["saved"] == {"id": saved["id"], "name": "docs"}


async def test_the_runs_of_a_saved_assignment_are_capped(app, store, monkeypatch) -> None:
    manager = get_manager()
    saved = await manager.save("alex", "docs", spec())
    for _ in range(4):
        await ended(manager, "alex", saved="docs")
    monkeypatch.setattr(read_api, "MAX_RUNS", 2)
    body = (await call(app, f"/v1/saved/{saved['id']}", person="alex")).json()
    assert [r["id"] for r in body["runs_list"]] == [4, 3]


async def test_another_persons_saved_assignment_is_404(app, store) -> None:
    saved = await get_manager().save("mia", "docs", spec())
    assert (await call(app, f"/v1/saved/{saved['id']}", person="alex")).status_code == 404
    assert (await call(app, "/v1/saved/999", person="alex")).status_code == 404
    assert (await call(app, "/v1/saved", person="alex")).json() == {"items": []}


async def test_no_answer_holds_a_token(app, store) -> None:
    saved = await get_manager().save("alex", "docs", spec())
    row = await make(store, finished_at=FIXED_NOW + timedelta(hours=1))
    texts = [
        (await call(app, path, person="alex")).text
        for path in ("/v1/assignments", f"/v1/assignments/{row['id']}", "/v1/saved")
    ]
    texts.append((await call(app, f"/v1/saved/{saved['id']}", person="alex")).text)
    for text in texts:
        for token in (VIEWER, LAPTOP, ALEX, ADDON, "assignments-fleet-token"):
            assert token not in text


async def test_a_write_method_is_refused(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/assignments",
            params={"person": "alex"},
            headers={"Authorization": f"Bearer {VIEWER}"},
        )
    assert response.status_code == 405
