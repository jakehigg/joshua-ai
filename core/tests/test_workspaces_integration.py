"""Workspaces against the real Postgres repo and the real routes: the schema,
the slugs, the scoping, the channel and conversation of a create, the items,
the nightly skip, the main conversation, and the usage by conversation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from joshua_core.engine.usage import Usage
from joshua_core.engine.workspace import WorkspaceBlock
from joshua_core.store.usage_events import UsageEvent
from joshua_core.transcript_api import build_transcript_router
from joshua_core.usage_api import build_usage_router
from joshua_core.workspaces_api import build_workspaces_router
from joshua_shared import config as config_module

pytestmark = pytest.mark.integration

VIEWER = "viewer-token"
MCP = "mcp-token"
CHANNELS = "channels-token"

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    role: member
  - id: mia
    name: Mia
    role: member
  - id: gus
    name: Gus
    role: guest
mcp:
  tasks:
    type: http
    url: "http://tasks/mcp"
  github:
    type: http
    url: "http://gh/mcp"
  private:
    type: http
    url: "http://p/mcp"
    allow:
      - mia
workspaces:
  models:
    - model-a
    - model-b
"""


@pytest.fixture(autouse=True)
def _tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)
    monkeypatch.setenv("JOSHUA_TOKEN_MCP", MCP)
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", CHANNELS)


@pytest.fixture
async def seeded(repo):
    await repo.upsert_person("alex", "Alex", "member")
    await repo.upsert_person("mia", "Mia", "member")
    yield repo
    # Leave no person behind: the boot test expects a store with none of mine.
    async with repo._db.pool.connection() as conn:
        await conn.execute("TRUNCATE people, channels, usage_event RESTART IDENTITY CASCADE")


def _app(repo, settings=None) -> FastAPI:
    settings = settings or config_module.parse(CONFIG, env={}, source="<test>")
    app = FastAPI()
    app.state.ctx = SimpleNamespace(
        settings=settings, repo=repo, manager=SimpleNamespace(workspace_block=None)
    )
    app.include_router(build_workspaces_router())
    app.include_router(build_transcript_router())
    app.include_router(build_usage_router(clock=lambda: datetime.now(UTC)))
    return app


@pytest.fixture
async def client(seeded):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(seeded)), base_url="http://core"
    ) as c:
        yield c


def _h(token: str = VIEWER) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _create(client, person="alex", **body: Any) -> dict[str, Any]:
    body.setdefault("title", "Solar install")
    resp = await client.post("/v1/workspaces", params={"person": person}, json=body, headers=_h())
    assert resp.status_code == 201, resp.text
    return resp.json()


# --- the schema and the repo -------------------------------------------------


async def test_the_schema_is_idempotent_and_rejects_bad_values(seeded) -> None:
    await seeded._db.init_schema()  # a second run adds nothing and fails nothing
    await seeded._db.init_schema()
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        await seeded.create_workspace("alex", "Bad notify", notify="always")
    ws = await seeded.create_workspace("alex", "Good")
    with pytest.raises(psycopg.errors.CheckViolation):
        await seeded.update_workspace(ws.id, status="deleted")
    with pytest.raises(psycopg.errors.CheckViolation):
        await seeded.add_workspace_item(ws.id, "photo", "r", "t")


async def test_create_makes_the_channel_the_conversation_and_unique_slugs(seeded) -> None:
    first = await seeded.create_workspace("alex", "Solar install!", goal="Go solar")
    second = await seeded.create_workspace("alex", "Solar install!")
    other = await seeded.create_workspace("mia", "Solar install!")
    odd = await seeded.create_workspace("alex", "???")

    assert (first.slug, second.slug, other.slug, odd.slug) == (
        "solar-install",
        "solar-install-2",
        "solar-install",
        "workspace",
    )
    channel = await seeded.get_channel(f"workspace:{first.id}")
    assert channel.channel_type == "workspace"
    assert channel.session_mode == "per_person"
    assert channel.default_person_id == "alex"
    conversation = await seeded.find_conversation(f"workspace:{first.id}", "alex")
    assert conversation is not None
    assert first.notify == "none" and first.status == "active" and first.servers == ()


async def test_find_scopes_to_the_owner_and_takes_a_slug_or_an_id(seeded) -> None:
    ws = await seeded.create_workspace("alex", "Trip")
    assert (await seeded.find_workspace("alex", "trip")).id == ws.id
    assert (await seeded.find_workspace("alex", ws.id)).id == ws.id
    assert await seeded.find_workspace("mia", "trip") is None
    assert await seeded.find_workspace("mia", ws.id) is None
    assert await seeded.find_workspace("alex", "") is None
    assert await seeded.find_workspace("alex", "not-a-uuid-or-slug") is None
    assert await seeded.get_workspace("nonsense") is None


async def test_items_update_in_place_and_order_pinned_then_newest(seeded) -> None:
    ws = await seeded.create_workspace("alex", "Trip")
    a = await seeded.add_workspace_item(ws.id, "document", "a", "First")
    await seeded.add_workspace_item(ws.id, "link", "b", "Second", url="https://b")
    await seeded.update_workspace_item(ws.id, a.id, pinned=True)
    again = await seeded.add_workspace_item(ws.id, "document", "a", "First, renamed")

    items = await seeded.list_workspace_items(ws.id)
    assert again.id == a.id
    assert [i.title for i in items] == ["First, renamed", "Second"]
    assert items[0].pinned is True  # the pin survives the update
    assert items[1].url == "https://b"


async def test_seen_flag_and_marking(seeded) -> None:
    ws = await seeded.create_workspace("alex", "Trip")
    await seeded.add_workspace_item(ws.id, "link", "new", "From chat", seen=False)
    await seeded.add_workspace_item(ws.id, "link", "old", "Old")
    unseen = await seeded.list_workspace_items(ws.id, unseen_only=True)
    assert [i.ref for i in unseen] == ["new"]
    # An update from inside the workspace does not hide a row that is still new.
    await seeded.add_workspace_item(ws.id, "link", "new", "From chat", seen=True)
    assert len(await seeded.list_workspace_items(ws.id, unseen_only=True)) == 1
    await seeded.mark_items_seen([])
    await seeded.mark_items_seen([i.id for i in await seeded.list_workspace_items(ws.id)])
    assert await seeded.list_workspace_items(ws.id, unseen_only=True) == []


async def test_item_calls_ignore_a_bad_id_and_another_workspace(seeded) -> None:
    ws = await seeded.create_workspace("alex", "Trip")
    other = await seeded.create_workspace("alex", "Other")
    item = await seeded.add_workspace_item(ws.id, "link", "a", "A")
    assert await seeded.update_workspace_item(ws.id, "nope", pinned=True) is None
    assert await seeded.delete_workspace_item(ws.id, "nope") is False
    assert await seeded.update_workspace_item(other.id, item.id, pinned=True) is None
    assert await seeded.delete_workspace_item(other.id, item.id) is False
    assert await seeded.delete_workspace_item(ws.id, item.id) is True


async def test_update_rejects_an_unknown_field(seeded) -> None:
    ws = await seeded.create_workspace("alex", "Trip")
    with pytest.raises(ValueError):
        await seeded.update_workspace(ws.id, owner="mia")
    assert (await seeded.update_workspace(ws.id)).id == ws.id


async def test_rollover_skips_workspace_conversations(seeded) -> None:
    ws = await seeded.create_workspace("alex", "Trip")
    await seeded.upsert_channel("telegram:1", "telegram")
    plain = await seeded.get_or_create_conversation("telegram:1", "alex")
    work = await seeded.find_conversation(f"workspace:{ws.id}", "alex")
    await seeded.set_sdk_session(plain.id, "s-plain")
    await seeded.set_sdk_session(work.id, "s-work")

    rolled = await seeded.rollover_sessions(date.today() + timedelta(days=1))

    assert rolled == 1
    assert (await seeded.get_conversation(plain.id)).sdk_session_id is None
    assert (await seeded.get_conversation(work.id)).sdk_session_id == "s-work"


async def test_clear_sdk_session(seeded) -> None:
    ws = await seeded.create_workspace("alex", "Trip")
    conv = await seeded.find_conversation(f"workspace:{ws.id}", "alex")
    await seeded.set_sdk_session(conv.id, "s1")
    await seeded.clear_sdk_session(conv.id)
    assert (await seeded.get_conversation(conv.id)).sdk_session_id is None


async def test_main_conversation_is_the_latest_direct_non_workspace_thread(seeded) -> None:
    assert await seeded.main_conversation("alex") is None
    ws = await seeded.create_workspace("alex", "Trip")  # newest, but a workspace
    await seeded.upsert_channel("telegram:1", "telegram", session_mode="per_person")
    await seeded.upsert_channel("telegram:-9", "telegram", session_mode="shared")
    await seeded.upsert_channel("voice:kitchen", "voice", session_mode="per_person")
    old = await seeded.get_or_create_conversation("telegram:1", "alex")
    await seeded.get_or_create_conversation("telegram:-9", None)
    await seeded.get_or_create_conversation("voice:kitchen", "alex")
    await seeded.touch_conversation(
        (await seeded.find_conversation(f"workspace:{ws.id}", "alex")).id
    )
    await seeded.touch_conversation(old.id, last_channel_id="imessage:+1")

    found = await seeded.main_conversation("alex")
    assert found.id == old.id and found.last_channel_id == "imessage:+1"
    assert await seeded.main_conversation("mia") is None


# --- the routes ------------------------------------------------------------------


async def test_the_bearer_the_list_and_the_person_are_checked(client) -> None:
    params = {"person": "alex"}
    assert (await client.get("/v1/workspaces", params=params)).status_code == 401
    assert (
        await client.get("/v1/workspaces", params=params, headers=_h("nope"))
    ).status_code == 401
    assert (await client.get("/v1/workspaces", params=params, headers=_h(MCP))).status_code == 403
    assert (await client.get("/v1/workspaces", headers=_h())).status_code == 400
    for person in ("nobody", "gus"):  # unknown, and a guest
        resp = await client.get("/v1/workspaces", params={"person": person}, headers=_h())
        assert resp.status_code == 403


async def test_an_empty_allowed_callers_turns_the_routes_off(seeded) -> None:
    settings = config_module.parse(CONFIG + "  allowed_callers:\n", env={}, source="<test>")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(seeded, settings)), base_url="http://core"
    ) as c:
        resp = await c.get("/v1/workspaces", params={"person": "alex"}, headers=_h())
    assert resp.status_code == 403


async def test_create_list_get_patch(client) -> None:
    made = await _create(
        client,
        goal=" Go solar ",
        model="model-a",
        servers=["tasks", "tasks", "github"],
        notify="questions",
    )
    assert made["slug"] == "solar-install" and made["goal"] == "Go solar"
    assert made["servers"] == ["tasks", "github"] and made["notify"] == "questions"
    assert made["channel"] == f"workspace:{made['id']}"

    listing = (await client.get("/v1/workspaces", params={"person": "alex"}, headers=_h())).json()
    assert [w["slug"] for w in listing["workspaces"]] == ["solar-install"]
    for ref in (made["slug"], made["id"]):
        got = await client.get(f"/v1/workspaces/{ref}", params={"person": "alex"}, headers=_h())
        assert got.json()["id"] == made["id"]

    patched = await client.patch(
        "/v1/workspaces/solar-install",
        params={"person": "alex"},
        headers=_h(),
        json={
            "notes": "Lauren is vegetarian",
            "status": "paused",
            "model": "",
            "servers": [],
            "title": "Solar!",
        },
    )
    body = patched.json()
    assert patched.status_code == 200
    assert (body["notes"], body["status"], body["model"], body["servers"]) == (
        "Lauren is vegetarian",
        "paused",
        None,
        [],
    )
    assert body["title"] == "Solar!" and body["slug"] == "solar-install"

    paused = await client.get(
        "/v1/workspaces", params={"person": "alex", "status": "paused"}, headers=_h()
    )
    assert len(paused.json()["workspaces"]) == 1
    active = await client.get(
        "/v1/workspaces", params={"person": "alex", "status": "active"}, headers=_h()
    )
    assert active.json()["workspaces"] == []
    bad = await client.get(
        "/v1/workspaces", params={"person": "alex", "status": "gone"}, headers=_h()
    )
    assert bad.status_code == 422


async def test_another_owner_gets_404_on_every_route(client) -> None:
    made = await _create(client)
    item = (
        await client.post(
            "/v1/workspaces/solar-install/items",
            params={"person": "alex"},
            headers=_h(),
            json={"kind": "link", "ref": "https://x", "title": "X"},
        )
    ).json()
    mia = {"person": "mia"}
    slug = made["slug"]
    calls = [
        client.get(f"/v1/workspaces/{slug}", params=mia, headers=_h()),
        client.get(f"/v1/workspaces/{made['id']}", params=mia, headers=_h()),
        client.patch(f"/v1/workspaces/{slug}", params=mia, headers=_h(), json={"goal": "x"}),
        client.get(f"/v1/workspaces/{slug}/items", params=mia, headers=_h()),
        client.post(
            f"/v1/workspaces/{slug}/items",
            params=mia,
            headers=_h(),
            json={"kind": "link", "ref": "r"},
        ),
        client.patch(
            f"/v1/workspaces/{slug}/items/{item['id']}",
            params=mia,
            headers=_h(),
            json={"pinned": True},
        ),
        client.delete(f"/v1/workspaces/{slug}/items/{item['id']}", params=mia, headers=_h()),
        client.get(f"/v1/workspaces/{slug}/block", params=mia, headers=_h()),
    ]
    for call in calls:
        assert (await call).status_code == 404
    mine = (await client.get("/v1/workspaces", params=mia, headers=_h())).json()
    assert mine["workspaces"] == []
    untouched = await client.get(f"/v1/workspaces/{slug}", params={"person": "alex"}, headers=_h())
    assert untouched.json()["goal"] == ""


async def test_validation_of_servers_model_and_bodies(client) -> None:
    mia = {"person": "mia"}
    alex = {"person": "alex"}
    post = lambda p, b: client.post("/v1/workspaces", params=p, json=b, headers=_h())  # noqa: E731
    # `private` is allowed for mia only.
    assert (await post(alex, {"title": "T", "servers": ["private"]})).status_code == 422
    assert (await post(mia, {"title": "T", "servers": ["private"]})).status_code == 201
    assert (await post(alex, {"title": "T", "servers": ["nope"]})).status_code == 422
    assert (await post(alex, {"title": "T", "model": "model-z"})).status_code == 422
    assert (await post(alex, {"title": ""})).status_code == 422
    assert (await post(alex, {"title": "T", "notify": "always"})).status_code == 422
    assert (await post(alex, {"title": "T", "extra": 1})).status_code == 422
    made = await _create(client)
    patch = lambda b: client.patch(  # noqa: E731
        f"/v1/workspaces/{made['slug']}", params=alex, json=b, headers=_h()
    )
    assert (await patch({"servers": ["nope"]})).status_code == 422
    assert (await patch({"model": "model-z"})).status_code == 422
    assert (await patch({"status": "gone"})).status_code == 422


async def test_any_model_is_accepted_when_the_list_is_empty(seeded) -> None:
    text = CONFIG.split("workspaces:")[0]
    settings = config_module.parse(text, env={}, source="<test>")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(seeded, settings)), base_url="http://core"
    ) as c:
        resp = await c.post(
            "/v1/workspaces",
            params={"person": "alex"},
            headers=_h(),
            json={"title": "T", "model": "anything-at-all"},
        )
    assert resp.status_code == 201 and resp.json()["model"] == "anything-at-all"


async def test_item_routes(client) -> None:
    await _create(client)
    base, params = "/v1/workspaces/solar-install/items", {"person": "alex"}
    added = await client.post(
        base,
        params=params,
        headers=_h(),
        json={"kind": "document", "ref": "quote", "title": "Quote", "url": "https://v/quote"},
    )
    item = added.json()
    assert added.status_code == 200 and item["added_by"] == "person" and item["seen"] is True
    # The same kind and ref updates the row.
    again = await client.post(
        base, params=params, headers=_h(), json={"kind": "document", "ref": "quote", "title": "New"}
    )
    assert again.json()["id"] == item["id"] and again.json()["title"] == "New"
    assert (
        await client.post(base, params=params, headers=_h(), json={"kind": "photo", "ref": "r"})
    ).status_code == 422

    pinned = await client.patch(
        f"{base}/{item['id']}", params=params, headers=_h(), json={"pinned": True}
    )
    assert pinned.json()["pinned"] is True
    assert (
        await client.patch(f"{base}/nope", params=params, headers=_h(), json={"pinned": True})
    ).status_code == 404
    listing = (await client.get(base, params=params, headers=_h())).json()["items"]
    assert [i["ref"] for i in listing] == ["quote"]
    assert (
        await client.delete(f"{base}/{item['id']}", params=params, headers=_h())
    ).status_code == 204
    assert (
        await client.delete(f"{base}/{item['id']}", params=params, headers=_h())
    ).status_code == 404


async def test_the_block_route_gives_the_block_text_and_marks_nothing(client, seeded) -> None:
    made = await _create(client, goal="Go solar")
    await seeded.update_workspace(made["id"], notes="Stay under budget")
    await seeded.add_workspace_item(made["id"], "plan", "p1", "Roof plan", seen=False)

    resp = await client.get(
        "/v1/workspaces/solar-install/block", params={"person": "alex"}, headers=_h()
    )
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/plain")
    text = resp.text
    assert text.startswith("[Workspace: Solar install (solar-install)]")
    assert "Goal: Go solar" in text and "Stay under budget" in text
    assert "- plan: Roof plan (p1)" in text and "New since your last turn" in text
    assert len(await seeded.list_workspace_items(made["id"], unseen_only=True)) == 1


async def test_the_block_provider_against_the_real_store(seeded) -> None:
    ws = await seeded.create_workspace("alex", "Trip", goal="Plan")
    conv = await seeded.find_conversation(ws.channel_id, "alex")
    await seeded.add_transcript(conv.id, "in", "where do we stay?")
    await seeded.add_transcript(conv.id, "out", "A cabin, maybe.")
    await seeded.add_workspace_item(ws.id, "link", "a", "Cabin", seen=False)
    channel = await seeded.get_channel(ws.channel_id)
    ctx = SimpleNamespace(channel=channel, conversation=conv, fresh_session=True, after_turn=[])

    text = await WorkspaceBlock(seeded)(ctx)

    assert "- alex: where do we stay?" in text and "- you: A cabin, maybe." in text
    assert "New since your last turn" in text
    for settle in ctx.after_turn:
        await settle()
    assert await seeded.list_workspace_items(ws.id, unseen_only=True) == []


# --- the transcript ----------------------------------------------------------------------


async def test_the_transcript_reads_a_workspace_by_slug_or_id_for_its_owner(client, seeded) -> None:
    made = await _create(client)
    conv = await seeded.find_conversation(f"workspace:{made['id']}", "alex")
    await seeded.add_transcript(conv.id, "in", "hello")
    for ref in (made["slug"], made["id"]):
        resp = await client.get(
            "/v1/transcript",
            params={"channel": f"workspace:{ref}", "person": "alex"},
            headers=_h(CHANNELS),
        )
        body = resp.json()
        assert body["conversation_id"] == conv.id
        assert [m["text"] for m in body["messages"]] == ["hello"]
    for params in (
        {"channel": f"workspace:{made['slug']}", "person": "mia"},
        {"channel": f"workspace:{made['slug']}"},
        {"channel": "workspace:nothing", "person": "alex"},
    ):
        body = (await client.get("/v1/transcript", params=params, headers=_h(CHANNELS))).json()
        assert body == {"conversation_id": None, "messages": [], "next_before": None}


# --- usage by conversation -------------------------------------------------------------------


async def test_usage_group_conversation(client, seeded) -> None:
    made = await _create(client)
    work = await seeded.find_conversation(f"workspace:{made['id']}", "alex")
    await seeded.upsert_channel("telegram:1", "telegram")
    plain = await seeded.get_or_create_conversation("telegram:1", "alex")
    for conversation, counts in ((work, (100, 10)), (work, (50, 5)), (plain, (7, 1))):
        await seeded.add_usage_event(
            UsageEvent(
                kind="chat",
                name="chat",
                usage=Usage(input=counts[0], output=counts[1]),
                conversation_id=conversation.id,
            )
        )
    await seeded.add_usage_event(
        UsageEvent(kind="reflection", name="reflection", usage=Usage(input=999))
    )

    resp = await client.get("/v1/usage", params={"group": "conversation"}, headers=_h())
    body = resp.json()
    rows = {r["conversation_id"]: r for r in body["conversations"]}

    assert resp.status_code == 200 and body["days"] == 30
    assert set(rows) == {work.id, plain.id}  # a row with no conversation is not listed
    assert rows[work.id]["workspace"] == {"slug": "solar-install", "title": "Solar install"}
    assert (rows[work.id]["calls"], rows[work.id]["input"], rows[work.id]["output"]) == (2, 150, 15)
    assert rows[plain.id]["workspace"] is None
    assert body["conversations"][0]["conversation_id"] == work.id  # the larger total first

    default = (await client.get("/v1/usage", headers=_h())).json()
    assert "groups" in default and "conversations" not in default  # unchanged without group
    bad = await client.get("/v1/usage", params={"group": "person"}, headers=_h())
    assert bad.status_code == 422
