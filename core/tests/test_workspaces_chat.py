"""Normal chat and workspaces, offline: the list and the hint, the four tools
of normal chat, the unseen rows in the block, the next steps, the jobs, and
the ``workspaces`` source."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from engine_fakes import make_settings
from joshua_core.engine import workspace as ws
from joshua_core.engine import workspace_awareness as aware
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools import workspace as ws_tools
from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk
from joshua_core.memory.sources.workspaces import (
    WorkspacesSource,
    person_of_uri,
    record_text,
    uri_of,
    workspace_id_of_uri,
)
from joshua_core.store.models import Channel, Conversation
from test_workspaces_engine import WS_ID, WsRepo, _channel, _item, _text, _ws

OTHER_ID = "22222222-2222-2222-2222-222222222222"
NORMAL = Channel(id="cli:alex", channel_type="cli", session_mode="per_person")


def _other(**fields: Any):
    base = dict(id=OTHER_ID, slug="turkey", title="Thanksgiving plans", goal="Cook for 12")
    base.update(fields)
    return _ws(**base)


def _deps(repo: WsRepo, channel: Channel = NORMAL, person_id: str = "alex") -> ToolDeps:
    return ToolDeps(
        repo=repo,
        conversation=Conversation(id="main", channel_id=channel.id, person_id=person_id),
        channel=channel,
        tz="UTC",
        person_id=person_id,
    )


def _repo() -> WsRepo:
    repo = WsRepo(_ws())
    repo.others = [_other()]
    return repo


# --- the list and the hint ------------------------------------------------------------------


def _turn(
    text: str = "hello there",
    channel: Channel = NORMAL,
    role: str = "member",
    person: str = "alex",
):
    return SimpleNamespace(
        channel=channel,
        conversation=Conversation(id="main", channel_id=channel.id, person_id=person),
        text=text,
        role=role,
        embedding=[0.1] * 4,
    )


class _Store:
    def __init__(self, rows: list[KbChunk]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def kb_search(self, person_id, embedding, *, k, min_sim, **kw):
        self.calls.append({"person": person_id, "min_sim": min_sim, **kw})
        return [r for r in self.rows if (r.similarity or 0) >= min_sim]


def _row(workspace_id: str, sim: float, person: str = "alex") -> KbChunk:
    return KbChunk(
        person_id=person,
        source="workspaces",
        kind="workspace",
        path=f"workspaces/{workspace_id}/{person}",
        title="x",
        heading="active",
        text="t",
        similarity=sim,
    )


async def test_the_list_names_active_workspaces_and_the_instruction() -> None:
    provider = aware.build_provider(_repo(), None, make_settings())
    note = await provider(_turn())
    assert "Solar install - Go solar" in note and "Thanksgiving plans - Cook for 12" in note
    assert "send_to_workspace" in note and "say so in one line" in note


async def test_an_owner_gets_the_list_as_a_member_does() -> None:
    """The member gate of the block admits the owner role."""
    note = await aware.build_provider(_repo(), None, make_settings())(_turn(role="owner"))
    assert note is not None and "Solar install - Go solar" in note


async def test_the_list_is_cut_to_ten_and_stays_short() -> None:
    repo = WsRepo(_ws(goal="g" * 500))
    repo.others = [_ws(id=str(i), slug=f"w{i}", title="T" * 200, goal="g" * 500) for i in range(15)]
    note = await aware.build_provider(repo, None, make_settings())(_turn())
    assert note.count("\n- ") == aware.MAX_LISTED
    assert len(note) < 1800  # about 300 tokens at most


async def test_nothing_is_added_without_an_active_workspace_or_for_other_chats() -> None:
    repo = WsRepo(_ws(status="paused"))
    assert await aware.build_provider(repo, None, make_settings())(_turn()) is None
    full = aware.build_provider(_repo(), None, make_settings())
    assert await full(_turn(role="guest")) is None
    group = Channel(id="telegram:-1", channel_type="telegram", session_mode="shared")
    assert await full(_turn(channel=group)) is None
    assert await full(_turn(channel=_channel())) is None


async def test_the_hint_needs_a_match_over_hint_sim() -> None:
    settings = make_settings()
    assert settings.workspaces.hint_sim == 0.70
    low, high = _Store([_row(OTHER_ID, 0.69)]), _Store([_row(OTHER_ID, 0.71)])
    text = "I found a great turkey brine recipe"
    no = await aware.build_provider(_repo(), low, settings)(_turn(text))
    yes = await aware.build_provider(_repo(), high, settings)(_turn(text))
    assert "May relate" not in no
    assert "May relate to workspace *Thanksgiving plans*." in yes
    assert high.calls[0]["kinds"] == ("workspace",) and high.calls[0]["min_sim"] == 0.70
    assert high.calls[0]["person"] == "alex"


async def test_the_hint_skips_a_workspace_that_is_not_active_and_short_messages(
    monkeypatch,
) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model: [0.1] * 4)
    store = _Store([_row("99999999-9999-9999-9999-999999999999", 0.9)])
    note = await aware.build_provider(_repo(), store, make_settings())(_turn("brine recipe now"))
    assert "May relate" not in note
    hit = _Store([_row(OTHER_ID, 0.9)])
    note = await aware.build_provider(_repo(), hit, make_settings())(_turn("ok"))
    assert "May relate" not in note and hit.calls == []


async def test_the_workspace_rows_are_not_returned_by_an_ordinary_search() -> None:
    from joshua_core.memory.search import search

    store = _Store([])
    await search(store, "alex", [0.1], k=3, min_sim=0.5)  # type: ignore[arg-type]
    assert "workspace" in store.calls[0]["exclude_kinds"]
    await search(store, "alex", [0.1], k=3, min_sim=0.5, kinds=("workspace",))  # type: ignore[arg-type]
    assert "workspace" not in (store.calls[1]["exclude_kinds"] or ())


# --- send_to_workspace --------------------------------------------------------------------


async def test_send_writes_a_row_and_unseen_items_and_runs_no_turn() -> None:
    repo = _repo()
    reply = await ws_tools.do_send_to_workspace(
        _deps(repo),
        workspace="THANKSGIVING plans",
        text="I like this turkey recipe",
        items=[{"kind": "link", "ref": "https://example.com/t", "title": "Turkey"}, {"kind": "x"}],
    )
    assert "Recorded in workspace 'Thanksgiving plans' with 1 item(s)" in _text(reply)
    assert "Not added" in _text(reply)
    assert repo.chat_rows[0]["content"] == "I like this turkey recipe"
    assert repo.added[0]["seen"] is False and repo.added[0]["workspace_id"] == OTHER_ID
    assert repo.transcripts == []  # no turn: nothing else was written


async def test_the_block_shows_the_rows_from_normal_chat_once() -> None:
    repo = WsRepo(_ws(), [])
    await ws_tools.do_send_to_workspace(_deps(repo), workspace="solar", text="quote came in $9k")
    ctx = SimpleNamespace(
        channel=_channel(),
        conversation=Conversation(id="c-ws", channel_id=_channel().id, person_id="alex"),
        fresh_session=False,
        after_turn=[],
    )
    block = ws.WorkspaceBlock(repo)
    first = await block(ctx)
    assert "From normal chat, since your last turn:" in first and "quote came in $9k" in first
    for settle in ctx.after_turn:
        await settle()
    ctx.after_turn.clear()
    assert "quote came in" not in await block(ctx)


async def test_a_status_read_does_not_mark_rows_seen() -> None:
    repo = WsRepo(_ws(), [])
    await ws_tools.do_send_to_workspace(_deps(repo), workspace="solar", text="note one")
    text = await ws.WorkspaceBlock(repo).render(_ws(), "c-ws", fresh=False, mark_seen=None)
    assert "note one" in text
    assert repo.chat_rows[0]["seen"] is False


async def test_send_refuses_an_archived_workspace_empty_text_and_other_chats() -> None:
    repo = WsRepo(_ws(status="archived"))
    assert "archived" in _text(
        await ws_tools.do_send_to_workspace(_deps(repo), workspace="solar", text="hi")
    )
    assert "Give the text" in _text(
        await ws_tools.do_send_to_workspace(_deps(repo), workspace="solar", text=" ")
    )
    assert "normal chat" in _text(
        await ws_tools.do_send_to_workspace(_deps(repo, _channel()), workspace="solar", text="hi")
    )
    assert repo.chat_rows == []


# --- tell_workspace -----------------------------------------------------------------------


async def test_tell_runs_one_turn_and_writes_no_unseen_row() -> None:
    repo, ran = _repo(), []

    async def run(workspace, text):
        ran.append((workspace.slug, text))

    reply = await ws_tools.do_tell_workspace(
        _deps(repo), workspace="turkey", text="Order the bird", run=run
    )
    assert ran == [("turkey", "[From normal chat] Order the bird")]
    assert "Told workspace 'Thanksgiving plans'" in _text(reply)
    assert repo.chat_rows == []


async def test_tell_refuses_a_paused_workspace_and_a_missing_runner() -> None:
    repo = WsRepo(_ws(status="paused"))

    async def run(workspace, text):
        raise AssertionError("no turn")

    assert "paused" in _text(
        await ws_tools.do_tell_workspace(_deps(repo), workspace="solar", text="go", run=run)
    )
    assert "not available" in _text(
        await ws_tools.do_tell_workspace(_deps(_repo()), workspace="solar", text="go", run=None)
    )


async def test_the_registered_runner_runs_an_inject_turn_and_delivers_nothing() -> None:
    repo = _repo()
    repo.get_channel = _channel_of  # type: ignore[method-assign]
    turns: list[dict[str, Any]] = []

    async def run_turn(channel, conversation, text, **kw):
        turns.append({"channel": channel.id, "text": text, **kw})

    delivered: list[Any] = []
    deliverer = SimpleNamespace(deliver=lambda *a: delivered.append(a))
    manager = SimpleNamespace(_repo=repo, run_turn=run_turn, registered={})
    manager.register_builtin = lambda name, factory, **kw: manager.registered.update(
        {name: factory}
    )
    ws_tools.register(manager, make_settings(), deliverer)
    seen: dict[str, Any] = {}
    ws_tools.create_sdk_mcp_server = lambda *, name, version, tools: seen.update(
        {t.name: t for t in tools}
    )
    manager.registered["workspace"](_deps(repo))
    reply = await seen["tell_workspace"].handler({"workspace": "turkey", "text": "Order it"})
    await asyncio.sleep(0.01)
    assert "Told workspace" in _text(reply)
    assert turns == [
        {
            "channel": f"workspace:{OTHER_ID}",
            "text": "[From normal chat] Order it",
            "direction": "inject",
        }
    ]
    assert delivered == []


async def _channel_of(channel_id: str):
    return Channel(id=channel_id, channel_type="workspace", default_person_id="alex")


@pytest.fixture(autouse=True)
def _restore_server_factory(monkeypatch):
    monkeypatch.setattr(ws_tools, "create_sdk_mcp_server", ws_tools.create_sdk_mcp_server)


# --- list and status, and the name matching -----------------------------------------------


async def test_list_gives_titles_slugs_statuses_and_goals() -> None:
    reply = _text(await ws_tools.do_list_workspaces(_deps(_repo())))
    assert "- Solar install (solar, active) - Go solar" in reply
    assert "- Thanksgiving plans (turkey, active) - Cook for 12" in reply
    empty = await ws_tools.do_list_workspaces(_deps(WsRepo(None)))
    assert "no workspaces" in _text(empty)


async def test_status_gives_the_block_and_never_the_thread() -> None:
    repo = _repo()
    repo.tail = [{"direction": "in", "content": "SECRET THREAD ROW", "status": "ok"}]

    async def render(workspace):
        return await ws.WorkspaceBlock(repo).render(workspace, "c-ws", fresh=False, mark_seen=None)

    reply = _text(await ws_tools.do_workspace_status(_deps(repo), workspace="Solar", render=render))
    assert reply.startswith("[Workspace: Solar install (solar)]")
    assert "SECRET THREAD ROW" not in reply


async def test_names_match_by_slug_or_title_without_case() -> None:
    repo = _repo()
    deps = _deps(repo)
    for name in ("turkey", "TURKEY", " thanksgiving PLANS "):
        found, _ = await ws_tools._match(deps, name)
        assert found is not None and found.slug == "turkey"
    assert await ws_tools._match(deps, "nowhere") == (None, [])


async def test_an_ambiguous_title_and_no_match_are_answered_clearly() -> None:
    repo = WsRepo(_ws(title="Garden"))
    repo.others = [_ws(id=OTHER_ID, slug="garden-2", title="garden")]
    ambiguous = _text(
        await ws_tools.do_send_to_workspace(_deps(repo), workspace="GARDEN", text="x")
    )
    assert "More than one workspace" in ambiguous and "garden-2" in ambiguous
    # A slug still picks one of them.
    assert "Recorded" in _text(
        await ws_tools.do_send_to_workspace(_deps(repo), workspace="garden-2", text="x")
    )
    none = _text(await ws_tools.do_send_to_workspace(_deps(_repo()), workspace="boat", text="x"))
    assert "No such workspace" in none and "solar, turkey" in none
    blank = _text(await ws_tools.do_send_to_workspace(_deps(_repo()), workspace="", text="x"))
    assert "Name the workspace" in blank


def test_a_group_chat_gets_none_of_the_tools() -> None:
    group = Channel(id="telegram:-1", channel_type="telegram", session_mode="shared")
    assert ws_tools.build_workspace_server(_deps(_repo(), group), make_settings()) is None


# --- a shared workspace in normal chat ----------------------------------------------------


SHARED_ID = "33333333-3333-3333-3333-333333333333"


def _shared_repo() -> WsRepo:
    """A repo whose only workspace is shared between alex (the owner) and mia."""
    repo = WsRepo(None)
    repo.others = [
        _ws(
            id=SHARED_ID,
            slug="garden",
            title="Garden plan",
            goal="Dig the beds",
            shared=True,
            members=("mia",),
        )
    ]
    return repo


async def test_a_shared_workspace_is_in_the_normal_chat_of_everybody_in_it() -> None:
    settings = make_settings()
    for person in ("alex", "mia"):
        store = _Store([_row(SHARED_ID, 0.9, person)])
        note = await aware.build_provider(_shared_repo(), store, settings)(
            _turn("the beds need compost", person=person)
        )
        assert note is not None and "Garden plan - Dig the beds" in note
        # The record of that person points back at the workspace of its uri.
        assert "May relate to workspace *Garden plan*." in note
    nobody = aware.build_provider(_shared_repo(), None, settings)
    assert await nobody(_turn(person="sam")) is None


async def test_tell_and_send_reach_a_shared_workspace_for_each_of_its_people() -> None:
    ran: list[tuple[str, str]] = []

    async def run(workspace, text):
        ran.append((workspace.slug, text))

    for person in ("alex", "mia"):
        repo = _shared_repo()
        deps = _deps(repo, person_id=person)
        told = await ws_tools.do_tell_workspace(
            deps, workspace="garden", text="Order seed", run=run
        )
        assert "Told workspace 'Garden plan'" in _text(told)
        sent = await ws_tools.do_send_to_workspace(deps, workspace="Garden PLAN", text="A quote")
        assert "Recorded in workspace 'Garden plan'" in _text(sent)
        # The row goes to the one thread of the workspace, which is nobody's.
        assert repo.conversations == [(f"workspace:{SHARED_ID}", None)]
        assert [r["content"] for r in repo.chat_rows] == ["A quote"]
    assert ran == [("garden", "[From normal chat] Order seed")] * 2
    outside = _text(
        await ws_tools.do_send_to_workspace(
            _deps(_shared_repo(), person_id="sam"), workspace="garden", text="x"
        )
    )
    assert "No such workspace" in outside


# --- next steps and jobs ------------------------------------------------------------------


def _factory(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_next_steps_are_off_without_a_url() -> None:
    assert ws.next_steps_section(WsRepo(_ws()), None) is None


async def test_next_steps_list_titles_and_numbers_for_a_linked_project() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"steps": [{"number": 12, "title": "Order panels"}, {"title": "Book roofer"}]}
        )

    repo = WsRepo(_ws(), [_item(kind="tasks_project", ref="solar-proj", title="Solar tasks")])
    section = ws.next_steps_section(repo, "http://tasks:8000", client_factory=_factory(handler))
    text = await section(_ws())
    assert "Next steps (Solar tasks):" in text
    assert "- #12 Order panels" in text and "- Book roofer" in text
    assert str(seen[0].url) == "http://tasks:8000/api/next-steps?project=solar-proj&limit=3"


async def test_next_steps_read_the_tasks_addon_answer_of_projects_with_tasks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": "p1",
                    "name": "Solar install",
                    "health": "on_track",
                    "tasks": [{"id": "t1", "number": 7, "title": "Order panels"}],
                }
            ],
        )

    repo = WsRepo(_ws(), [_item(kind="tasks_project", ref="p1", title="Solar tasks")])
    section = ws.next_steps_section(repo, "http://tasks:8000", client_factory=_factory(handler))
    text = await section(_ws())
    assert "- #7 Order panels" in text and "Solar install" not in text


async def test_next_steps_skip_on_an_error_a_timeout_or_no_project() -> None:
    def broken(request):
        return httpx.Response(500)

    def slow(request):
        raise httpx.ReadTimeout("slow")

    repo = WsRepo(_ws(), [_item(kind="tasks_project", ref="p", title="P")])
    for handler in (broken, slow):
        section = ws.next_steps_section(repo, "http://tasks:8000", client_factory=_factory(handler))
        assert await section(_ws()) is None
    none = ws.next_steps_section(WsRepo(_ws(), []), "http://t", client_factory=_factory(broken))
    assert await none(_ws()) is None


async def test_the_next_steps_ride_in_the_block_through_add_section() -> None:
    def handler(request):
        return httpx.Response(200, json=[{"number": 3, "title": "Call the city"}])

    repo = WsRepo(_ws(), [_item(kind="tasks_project", ref="p", title="P")])
    block = ws.WorkspaceBlock(repo)
    block.add_section(ws.next_steps_section(repo, "http://t", client_factory=_factory(handler)))
    text = await block.render(_ws(), "c-ws", fresh=False)
    assert "- #3 Call the city" in text


async def test_the_jobs_section_lists_job_items_with_their_dates_and_no_status() -> None:
    old = datetime(2026, 11, 1, tzinfo=UTC)
    new = datetime(2026, 11, 9, tzinfo=UTC)
    repo = WsRepo(
        _ws(),
        [
            _item(kind="developer_task", ref="t-1", title="Fix roof calc", added_at=old),
            _item(kind="assignment", ref="a-7", title="Audit quotes", added_at=new),
            _item(kind="document", ref="d", title="Quote"),
        ],
    )
    text = await ws.jobs_section(repo)(_ws())
    lines = text.splitlines()
    assert "status is not tracked here" in lines[0]
    assert lines[1] == "- assignment: Audit quotes (a-7, added 2026-11-09)"
    assert lines[2] == "- developer_task: Fix roof calc (t-1, added 2026-11-01)"
    assert len(lines) == 3
    assert await ws.jobs_section(WsRepo(_ws(), [_item()]))(_ws()) is None


# --- the source ---------------------------------------------------------------------------


class _SourceRepo(WsRepo):
    async def list_all_workspaces(self, status=None):
        rows = [w for w in [self.workspace, *self.others] if w]
        return [w for w in rows if status is None or w.status == status]


async def test_the_source_has_one_record_per_active_workspace() -> None:
    repo = _SourceRepo(
        _ws(notes="Budget 20k"),
        [_item(title="Roof quote"), _item(ref="b", title="Panel spec")],
    )
    repo.others = [_other(status="paused")]
    source = WorkspacesSource(repo)
    docs = [d async for d in source.list_documents()]
    assert [d.uri for d in docs] == [f"workspaces/{WS_ID}/alex"]
    doc = docs[0]
    assert doc.person_id == "alex" and doc.title == "Solar install"
    assert "Goal: Go solar" in doc.text and "Notes: Budget 20k" in doc.text
    assert "Items: Roof quote; Panel spec" in doc.text
    assert await source.fetch(f"workspaces/{OTHER_ID}/alex") is None
    assert await source.fetch(f"workspaces/{WS_ID}/mia") is None
    assert (await source.fetch(f"workspaces/{WS_ID}/alex")).rev == doc.rev


async def test_the_source_has_one_record_for_each_person_of_a_shared_workspace() -> None:
    repo = _SourceRepo(_ws(shared=True, members=("mia",)), [_item(title="Roof quote")])
    source = WorkspacesSource(repo)

    docs = [d async for d in source.list_documents()]

    assert [d.uri for d in docs] == [f"workspaces/{WS_ID}/alex", f"workspaces/{WS_ID}/mia"]
    assert [d.person_id for d in docs] == ["alex", "mia"]
    assert docs[0].text == docs[1].text and docs[0].rev == docs[1].rev
    assert (await source.fetch(f"workspaces/{WS_ID}/mia")).person_id == "mia"
    assert await source.fetch(f"workspaces/{WS_ID}/sam") is None
    assert await source.fetch(f"workspaces/{WS_ID}") is None


def test_the_uri_names_the_workspace_and_the_person() -> None:
    assert uri_of(WS_ID, "mia") == f"workspaces/{WS_ID}/mia"
    assert workspace_id_of_uri(f"workspaces/{WS_ID}/mia") == WS_ID
    assert person_of_uri(f"workspaces/{WS_ID}/mia") == "mia"
    assert workspace_id_of_uri(f"wiki/{WS_ID}/mia") is None
    assert person_of_uri(f"workspaces/{WS_ID}") is None


async def test_the_record_revision_follows_the_content() -> None:
    repo = _SourceRepo(_ws(), [_item(title="A")])
    first = [d async for d in WorkspacesSource(repo).list_documents()][0]
    repo.items = [_item(title="A"), _item(ref="b", title="B")]
    second = [d async for d in WorkspacesSource(repo).list_documents()][0]
    assert first.rev != second.rev


def test_the_record_text_is_cut() -> None:
    text = record_text(_ws(goal="g " * 2000), [])
    assert len(text) <= 1501
