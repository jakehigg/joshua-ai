"""Comments on a definition: the routes, each auth outcome, the person scope,
stale detection, the counts, and the two tools."""

from __future__ import annotations

import httpx
import pytest
from conftest import ADDON, ALEX, LAPTOP, MIA, error, mcp_session, payload, spec
from joshua_assignments import comments as comments_mod
from joshua_assignments import definition
from joshua_assignments.server import get_manager
from joshua_assignments.store import Parent


async def request(
    app, method: str, path: str, token: str | None = "viewer-fleet-token", json=None, **params
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, params=params, headers=headers, json=json)


async def saved(person: str = "alex", name: str = "docs", **fields) -> dict:
    return await get_manager().save(person, name, spec(title="Docs", **fields))


async def run(store, person: str = "alex", status: str = "done") -> dict:
    return await store.create(
        person_id=person,
        conversation_id=None,
        spec={"goal": "Clean up the docs.", "model": "sonnet"},
        model="claude-sonnet-5",
        status=status,
    )


def lines_of(spec_: dict) -> list[str]:
    return definition.lines(spec_)


async def post(app, base: str, line_no: int, line_text: str, body: str = "Delete this line."):
    return await request(
        app,
        "POST",
        f"{base}/comments",
        json={"line_no": line_no, "line_text": line_text, "body": body},
        person="alex",
    )


@pytest.fixture
async def parents(app, store) -> dict[str, tuple[str, list[str]]]:
    """A saved assignment and a run of alex: the route base and the definition lines."""
    kept = await saved()
    row = await run(store)
    saved_spec = (await store.get_saved(kept["id"]))["spec"]
    return {
        "saved": (f"/v1/saved/{kept['id']}", lines_of(saved_spec)),
        "assignment": (f"/v1/assignments/{row['id']}", lines_of(row["spec"])),
    }


KINDS = ["saved", "assignment"]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_comment_round_trip(app, parents, kind) -> None:
    base, lines = parents[kind]
    created = await post(app, base, 2, lines[1], "  Make this <b>shorter</b>.  ")
    assert created.status_code == 201
    body = created.json()
    assert set(body) == {
        "id",
        "target",
        "line_no",
        "line_text",
        "line_now",
        "body",
        "created_at",
        "resolved_at",
        "resolution",
        "stale",
    }
    assert body["body"] == "Make this <b>shorter</b>."
    assert (body["target"], body["line_no"], body["stale"]) == ("definition", 2, False)
    listed = (await request(app, "GET", f"{base}/comments", person="alex")).json()
    assert listed == {"items": [body]}
    detail = (await request(app, "GET", base, person="alex")).json()
    assert detail["comments"] == [body] and detail["open_comments"] == 1
    assert detail["definition"].split("\n") == lines


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("token", [None, "nope", ALEX, ADDON])
async def test_a_bearer_that_is_not_a_read_caller_cannot_comment(
    app, store, parents, kind, token
) -> None:
    base, lines = parents[kind]
    for method, path, json in (
        ("GET", f"{base}/comments", None),
        ("POST", f"{base}/comments", {"line_no": 1, "line_text": lines[0], "body": "x"}),
        ("DELETE", f"{base}/comments/1", None),
    ):
        response = await request(app, method, path, token=token, json=json, person="alex")
        assert response.status_code == 401
    assert store.comment_rows == {}


async def test_a_laptop_token_is_refused_by_the_default_read_callers(app, parents) -> None:
    base, lines = parents["saved"]
    response = await request(
        app,
        "POST",
        f"{base}/comments",
        token=LAPTOP,
        json={"line_no": 1, "line_text": lines[0], "body": "x"},
        person="alex",
    )
    assert response.status_code == 401


@pytest.mark.parametrize("kind", KINDS)
async def test_a_missing_person_is_400(app, parents, kind) -> None:
    base, _ = parents[kind]
    assert (await request(app, "GET", f"{base}/comments")).status_code == 400
    assert (await request(app, "POST", f"{base}/comments", json={})).status_code == 400


@pytest.mark.parametrize("kind", KINDS)
async def test_another_person_cannot_read_or_write_the_comments(app, store, parents, kind) -> None:
    base, lines = parents[kind]
    await post(app, base, 1, lines[0])
    for method, path, json in (
        ("GET", base, None),
        ("GET", f"{base}/comments", None),
        ("POST", f"{base}/comments", {"line_no": 1, "line_text": lines[0], "body": "x"}),
        ("DELETE", f"{base}/comments/1", None),
    ):
        response = await request(app, method, path, json=json, person="mia")
        assert response.status_code == 404, (method, path)
    assert len(store.comment_rows) == 1
    assert all(row["person_id"] == "alex" for row in store.comment_rows.values())


async def test_an_unknown_parent_is_404(app) -> None:
    for path in ("/v1/saved/99/comments", "/v1/assignments/99/comments"):
        assert (await request(app, "GET", path, person="alex")).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        [],
        {"line_no": 0, "line_text": "x", "body": "x"},
        {"line_no": 999, "line_text": "x", "body": "x"},
        {"line_no": True, "line_text": "x", "body": "x"},
        {"line_no": "1", "line_text": "x", "body": "x"},
        {"line_no": 1, "body": "x"},
        {"line_no": 1, "line_text": "x", "body": "   "},
        {"line_no": 1, "line_text": "x", "body": "x" * 2001},
        {"line_no": 1, "line_text": "x" * 20101, "body": "x"},
        {"line_no": 1, "line_text": "x", "body": "x", "target": "report"},
    ],
)
async def test_a_bad_comment_is_400(app, store, parents, body) -> None:
    base, _ = parents["saved"]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        kwargs = {"content": body} if isinstance(body, str) else {"json": body}
        response = await client.post(
            f"{base}/comments",
            params={"person": "alex"},
            headers={"Authorization": "Bearer viewer-fleet-token"},
            **kwargs,
        )
    assert response.status_code == 400
    assert store.comment_rows == {}


async def test_the_open_comments_of_a_definition_are_capped(app, parents, monkeypatch) -> None:
    from joshua_assignments import read_api

    monkeypatch.setattr(read_api, "MAX_OPEN_COMMENTS", 2)
    base, lines = parents["saved"]
    assert (await post(app, base, 1, lines[0])).status_code == 201
    assert (await post(app, base, 1, lines[0])).status_code == 201
    assert (await post(app, base, 1, lines[0])).status_code == 409


async def test_a_comment_is_stale_when_its_line_changes(app, store, parents) -> None:
    base, lines = parents["saved"]
    title_line = lines.index("title: Docs") + 1
    await post(app, base, title_line, "title: Docs")
    assert (await request(app, "GET", f"{base}/comments", person="alex")).json()["items"][0][
        "stale"
    ] is False
    await get_manager().save("alex", "docs", spec(title="Docs, renamed"))
    [item] = (await request(app, "GET", f"{base}/comments", person="alex")).json()["items"]
    assert item["stale"] is True and item["line_text"] == "title: Docs"


async def test_a_comment_whose_line_is_gone_is_stale(app, store) -> None:
    kept = await saved(repos=["gitlab.example.net/infra/docs"])
    base = f"/v1/saved/{kept['id']}"
    lines = lines_of((await store.get_saved(kept["id"]))["spec"])
    await post(app, base, len(lines), lines[-1])
    await get_manager().save("alex", "docs", {"goal": "Short."})
    [item] = (await request(app, "GET", f"{base}/comments", person="alex")).json()["items"]
    assert item["stale"] is True


async def test_the_saved_list_counts_the_open_comments(app, store, parents) -> None:
    base, lines = parents["saved"]
    await post(app, base, 1, lines[0])
    await post(app, base, 2, lines[1])
    await saved(name="other")
    await saved(person="mia", name="docs")
    items = (await request(app, "GET", "/v1/saved", person="alex")).json()["items"]
    assert {i["name"]: i["open_comments"] for i in items} == {"docs": 2, "other": 0}
    [first, _] = sorted(store.comment_rows)
    await store.resolve_comment("alex", first, "Done.")
    items = (await request(app, "GET", "/v1/saved", person="alex")).json()["items"]
    assert {i["name"]: i["open_comments"] for i in items}["docs"] == 1
    detail = (await request(app, "GET", base, person="alex")).json()
    assert detail["open_comments"] == 1 and len(detail["comments"]) == 2
    assert detail["comments"][0]["resolution"] == "Done."


@pytest.mark.parametrize("kind", KINDS)
async def test_the_author_deletes_an_open_comment(app, store, parents, kind) -> None:
    base, lines = parents[kind]
    cid = (await post(app, base, 1, lines[0])).json()["id"]
    response = await request(app, "DELETE", f"{base}/comments/{cid}", person="alex")
    assert response.status_code == 204
    assert store.comment_rows == {}
    again = await request(app, "DELETE", f"{base}/comments/{cid}", person="alex")
    assert again.status_code == 404


async def test_a_resolved_comment_cannot_be_deleted(app, store, parents) -> None:
    base, lines = parents["saved"]
    cid = (await post(app, base, 1, lines[0])).json()["id"]
    await store.resolve_comment("alex", cid, "Done.")
    response = await request(app, "DELETE", f"{base}/comments/{cid}", person="alex")
    assert response.status_code == 409
    assert cid in store.comment_rows


async def test_a_comment_is_deleted_only_through_its_own_parent(app, store, parents) -> None:
    base, lines = parents["saved"]
    cid = (await post(app, base, 1, lines[0])).json()["id"]
    other, _ = parents["assignment"]
    response = await request(app, "DELETE", f"{other}/comments/{cid}", person="alex")
    assert response.status_code == 404
    assert cid in store.comment_rows


async def test_a_comment_of_another_person_cannot_be_deleted(app, store, parents) -> None:
    theirs = await saved(person="mia", name="docs")
    row = await store.add_comment(
        "mia",
        Parent("saved", theirs["id"]),
        target="definition",
        line_no=1,
        line_text="x",
        body="y",
    )
    base, _ = parents["saved"]
    for path in (f"/v1/saved/{theirs['id']}/comments/{row['id']}", f"{base}/comments/{row['id']}"):
        assert (await request(app, "DELETE", path, person="alex")).status_code == 404
    assert row["id"] in store.comment_rows


# --- the tools ---------------------------------------------------------------


async def test_assignment_comments_and_resolve_comment(app, store, parents) -> None:
    base, lines = parents["saved"]
    title_line = lines.index("title: Docs") + 1
    first = (await post(app, base, title_line, "title: Docs", "Rename it.")).json()
    second = (await post(app, base, 1, "not the line", "Keep it.")).json()
    async with mcp_session(app) as session:
        got = payload(await session.call_tool("assignment_comments", {"saved": "docs"}))
        assert got["saved"]["name"] == "docs" and got["id"] is None
        assert got["definition"].split("\n") == lines
        assert got["spec"]["title"] == "Docs"
        assert [c["id"] for c in got["comments"]] == [first["id"], second["id"]]
        one, two = got["comments"]
        assert (one["body"], one["line_now"], one["stale"]) == ("Rename it.", "title: Docs", False)
        assert two["stale"] is True and two["line_now"] == lines[0]
        assert "save_assignment('docs', spec)" in got["message"]
        assert "request of the person" in got["message"]
        done = payload(
            await session.call_tool(
                "resolve_comment", {"comment_id": first["id"], "resolution": "Renamed\nit."}
            )
        )
        assert done["status"] == "resolved" and done["resolution"] == "Renamed it."
        again = payload(
            await session.call_tool(
                "resolve_comment", {"comment_id": first["id"], "resolution": "x"}
            )
        )
        assert again["reason"] == "resolved"
        left = payload(await session.call_tool("assignment_comments", {"saved": "docs"}))
    assert [c["id"] for c in left["comments"]] == [second["id"]]
    assert store.comment_rows[first["id"]]["resolution"] == "Renamed it."


async def test_assignment_comments_of_a_run(app, store, parents) -> None:
    base, lines = parents["assignment"]
    await post(app, base, 1, lines[0])
    run_id = int(base.rsplit("/", 1)[1])
    async with mcp_session(app) as session:
        got = payload(await session.call_tool("assignment_comments", {"id": run_id}))
    assert got["id"] == run_id and got["saved"] is None
    assert len(got["comments"]) == 1
    assert "start a new run" in got["message"]


@pytest.mark.parametrize("args", [{}, {"saved": "docs", "id": 1}])
async def test_assignment_comments_needs_one_of_saved_and_id(app, parents, args) -> None:
    async with mcp_session(app) as session:
        got = payload(await session.call_tool("assignment_comments", args))
    assert got["reason"] == "invalid_arguments"


async def test_the_tools_do_not_reach_the_comments_of_another_person(app, store, parents) -> None:
    base, lines = parents["saved"]
    cid = (await post(app, base, 1, lines[0])).json()["id"]
    run_id = int(parents["assignment"][0].rsplit("/", 1)[1])
    async with mcp_session(app, token=MIA) as session:
        none = payload(await session.call_tool("assignment_comments", {"saved": "docs"}))
        refused = payload(
            await session.call_tool("resolve_comment", {"comment_id": cid, "resolution": "x"})
        )
        theirs = await session.call_tool("assignment_comments", {"id": run_id})
    assert none["reason"] == "not_found"
    assert refused["reason"] == "not_found"
    assert error(theirs).endswith(f"404 not found: no assignment {run_id}")
    assert store.comment_rows[cid]["resolved_at"] is None


@pytest.mark.parametrize("resolution", ["", "   ", "x" * 301, 5])
async def test_a_bad_resolution_is_refused(app, store, parents, resolution) -> None:
    base, lines = parents["saved"]
    cid = (await post(app, base, 1, lines[0])).json()["id"]
    got = await get_manager().resolve_comment("alex", cid, resolution)
    assert got["reason"] == "invalid_arguments"
    assert (await get_manager().resolve_comment("alex", "1", "x"))["reason"] == "invalid_arguments"
    assert store.comment_rows[cid]["resolved_at"] is None


async def test_assignment_comments_with_a_bad_or_unknown_name(app) -> None:
    manager = get_manager()
    assert (await manager.open_comments("alex", saved="Bad Name"))["reason"] == "invalid_arguments"
    assert (await manager.open_comments("alex", saved="nothing"))["reason"] == "not_found"
    empty = await manager.save("alex", "empty", spec())
    got = await manager.open_comments("alex", saved="empty")
    assert got["comments"] == [] and got["message"] == "No open comment."
    assert got["saved"] == {"id": empty["id"], "name": "empty"}


def test_a_new_comment_is_checked() -> None:
    lines = {"definition": ["a", "b"]}
    assert comments_mod.new_comment(
        {"line_no": 2, "line_text": "b", "body": " x\r\ny "}, lines
    ) == {
        "target": "definition",
        "line_no": 2,
        "line_text": "b",
        "body": "x\ny",
    }
    with pytest.raises(comments_mod.CommentRefused):
        comments_mod.new_comment("x", lines)
