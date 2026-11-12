"""Workspaces in the engine, offline: the session build, the block, the cap,
RAG exclude, the delivery gate, the profile, and the three builtin tools.

A fake repo stands in for Postgres and a fake SDK client for the model.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from engine_fakes import (
    FakeComposer,
    FakeRepo,
    FakeSDKClient,
    install_fake_sdk,
    make_settings,
    resolved,
)
from joshua_core.delivery import workspace_delivery_target
from joshua_core.engine import workspace as ws
from joshua_core.engine.injection import kb_context
from joshua_core.engine.manager import ConversationManager, TurnCtx
from joshua_core.engine.profiles import derive_profile
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools import workspace as ws_tools
from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk
from joshua_core.store.models import Channel, Conversation, Person, Workspace, WorkspaceItem
from joshua_shared.inference import MODEL_TIER_KEYS, TOOL_SEARCH_KEY, TOOL_SEARCH_ON

MCP = """
mcp:
  files:
    kind: builtin
    allow: all
  tasks:
    type: http
    url: "http://tasks/mcp"
  github:
    type: http
    url: "http://gh/mcp"
  gitlab:
    type: http
    url: "http://gl/mcp"
  secret:
    type: http
    url: "http://s/mcp"
    allow:
      - mia
workspaces:
  context_cap: 5000
  tool_search_max_servers: 2
  models:
    - model-a
    - model-b
  viewer_url: https://joshua.example.org/
"""

WS_ID = "11111111-1111-1111-1111-111111111111"


def _ws(**fields: Any) -> Workspace:
    base = dict(id=WS_ID, slug="solar", title="Solar install", owner="alex", goal="Go solar")
    base.update(fields)
    return Workspace(**base)


def _item(kind="document", ref="quote", title="Quote", **fields: Any) -> WorkspaceItem:
    base = dict(id=f"i-{ref}", workspace_id=WS_ID, kind=kind, ref=ref, title=title)
    base.update(fields)
    return WorkspaceItem(**base)


def _channel(channel_type="workspace") -> Channel:
    return Channel(
        id=f"{channel_type}:{WS_ID}", channel_type=channel_type, default_person_id="alex"
    )


class WsRepo(FakeRepo):
    """The fake repo with the workspace calls the engine makes."""

    def __init__(self, workspace: Workspace | None = None, items=None) -> None:
        super().__init__()
        self.workspace = workspace
        self.items: list[WorkspaceItem] = list(items or [])
        self.tail: list[dict[str, Any]] = []
        self.cleared: list[str] = []
        self.seen: list[str] = []
        self.added: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.main: Conversation | None = None
        self.others: list[Workspace] = []

    async def get_workspace(self, workspace_id: str):
        return self.workspace if self.workspace and self.workspace.id == workspace_id else None

    async def find_workspace(self, owner: str, ref: str):
        for w in [self.workspace, *self.others]:
            if w and w.owner == owner and ref in (w.slug, w.id):
                return w
        return None

    async def list_workspaces(self, owner: str, status: str | None = None):
        return [w for w in [self.workspace, *self.others] if w and w.owner == owner]

    async def list_workspace_items(self, workspace_id: str, *, unseen_only: bool = False):
        return list(self.items)

    async def transcript_tail(self, conversation_id: str, limit: int = 40):
        return self.tail[-limit:]

    async def mark_items_seen(self, ids):
        self.seen.extend(ids)

    async def clear_sdk_session(self, conversation_id: str):
        self.cleared.append(conversation_id)

    async def add_workspace_item(self, workspace_id, kind, ref, title, **kw):
        self.added.append({"workspace_id": workspace_id, "kind": kind, "ref": ref, **kw})

    async def update_workspace(self, workspace_id, **fields):
        self.notes.append(fields["notes"])

    async def main_conversation(self, person_id: str):
        return self.main


def _derive(person, channel):
    return derive_profile(person, channel)


def make_manager(repo, tmp_path, settings=None) -> ConversationManager:
    return ConversationManager(
        repo=repo,
        settings=settings or make_settings(mcp=MCP),
        composer=FakeComposer(),
        derive_profile=_derive,
        gateway_url="http://gw:8000",
        gateway_token="tok",
        agent_backend="sdk",
        data_dir=tmp_path,
    )


def _conversation(sdk: str | None = None) -> Conversation:
    return Conversation(
        id="c-ws", channel_id=f"workspace:{WS_ID}", person_id="alex", sdk_session_id=sdk
    )


# --- the profile --------------------------------------------------------------


def test_workspace_channel_adds_the_prompt_file_to_the_dm_profile() -> None:
    person = Person(id="alex", display_name="Alex")
    plain = derive_profile(person, Channel(id="cli:alex", channel_type="cli"))
    profile = derive_profile(person, _channel())
    assert profile.name == "dm"
    assert profile.prompt_files == (*plain.prompt_files, "workspace.md")
    assert "workspace.md" not in plain.prompt_files


# --- the session build ----------------------------------------------------------


async def test_session_build_uses_the_model_servers_and_tool_search(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    workspace = _ws(model="model-b", servers=("tasks", "github", "secret", "nope"))
    manager = make_manager(WsRepo(workspace), tmp_path)

    built = await manager._build_session(_channel(), _conversation(), resume=None)

    options = built.session._options
    # The workspace picks tasks and github; mia-only `secret` and the unknown
    # name are dropped, and files (not picked) is out. No builtin is registered.
    assert list(options.mcp_servers) == ["tasks", "github"]
    assert options.model == "model-b"
    assert built.inference.model == "model-b"
    # Two picked servers, limit two: tool search is off.
    assert options.tools == []
    assert options.env[TOOL_SEARCH_KEY] == ""
    assert built.workspace_sig == ("model-b", ("tasks", "github", "secret", "nope"))


async def test_more_servers_than_the_limit_turn_tool_search_on(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    workspace = _ws(servers=("tasks", "github", "gitlab"))
    manager = make_manager(WsRepo(workspace), tmp_path)

    built = await manager._build_session(_channel(), _conversation(), resume=None)

    options = built.session._options
    assert options.tools == ["ToolSearch"]
    assert options.env[TOOL_SEARCH_KEY] == TOOL_SEARCH_ON
    assert built.inference.tool_search is True
    # No model picked: the model of the chat job stays.
    assert built.inference.model == make_settings(mcp=MCP).core.model


async def test_a_normal_chat_session_is_unchanged(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    repo = WsRepo(_ws(servers=("tasks",)))
    manager = make_manager(repo, tmp_path)
    channel = Channel(id="cli:alex", channel_type="cli")

    built = await manager._build_session(channel, _conversation(), resume=None)

    assert set(built.session._options.mcp_servers) == {"files", "tasks", "github", "gitlab"}
    assert built.workspace_sig is None


async def test_a_missing_workspace_row_builds_a_normal_session(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    manager = make_manager(WsRepo(None), tmp_path)
    built = await manager._build_session(_channel(), _conversation(), resume=None)
    assert "files" in built.session._options.mcp_servers
    assert built.workspace_sig is None


def test_with_workspace_rewrites_the_model_tiers_on_another_server() -> None:
    env = {key: "old" for key in MODEL_TIER_KEYS}
    base = resolved(model="old", env=env)
    out = ws.with_workspace(base, _ws(model="new"), tool_search=False)
    assert out.model == "new"
    assert all(out.env[key] == "new" for key in MODEL_TIER_KEYS)
    # On the subscription the tiers are empty and stay empty.
    plain = ws.with_workspace(resolved(model="old"), _ws(model="new"), tool_search=True)
    assert plain.env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == ""
    assert plain.env[TOOL_SEARCH_KEY] == TOOL_SEARCH_ON


async def test_a_changed_workspace_rebuilds_the_session(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    repo = WsRepo(_ws(servers=("tasks",)))
    manager = make_manager(repo, tmp_path)
    mc = await manager._get_or_create(_channel(), _conversation())
    first = mc.session

    again = await manager._get_or_create(_channel(), _conversation())
    assert again.session is first  # same servers and model: kept

    repo.workspace = replace(repo.workspace, servers=("tasks", "github"))
    changed = await manager._get_or_create(_channel(), _conversation())
    assert changed.session is not first
    assert list(changed.session._options.mcp_servers) == ["tasks", "github"]


# --- the block ---------------------------------------------------------------------


def test_the_block_lists_pinned_first_then_newest_and_caps_at_thirty() -> None:
    items = [_item(ref="pin", title="Pinned plan", kind="plan", pinned=True)] + [
        _item(ref=f"d{i}", title=f"Doc {i}") for i in range(40)
    ]
    text = ws.render_block(_ws(notes="Lauren is vegetarian"), items)
    lines = text.splitlines()
    assert lines[0] == "[Workspace: Solar install (solar)]"
    assert "Goal: Go solar" in lines
    assert "Notify: none" in lines
    assert "Notes:" in lines and "Lauren is vegetarian" in lines
    assert "Items (30 of 41):" in lines
    listed = [line for line in lines if line.startswith("- ")]
    assert listed[0] == "- [pinned] plan: Pinned plan (pin)"
    assert listed[1].startswith("- document: Doc 0")
    assert len(listed) == 30


def test_the_block_shows_unseen_items_and_recent_rows_after_the_items() -> None:
    new = _item(ref="recipe", title="Turkey recipe", kind="link")
    text = ws.render_block(
        _ws(), [_item()], unseen=[new], recent=["- alex: hello"], extra=["Next steps: none"]
    )
    order = [
        text.index("Items ("),
        text.index("New since your last turn"),
        text.index("Next steps: none"),
        text.index("The last rows"),
    ]
    assert order == sorted(order)
    assert "- link: Turkey recipe (recipe)" in text


def test_the_block_without_items() -> None:
    assert "Items: none yet." in ws.render_block(_ws(), [])


def test_recent_lines_skip_failed_blank_and_silent_rows_and_cut_long_text() -> None:
    rows = [
        {"direction": "in", "content": "hi", "status": "ok"},
        {"direction": "out", "content": "[error] boom", "status": "failed"},
        {"direction": "out", "content": "[IGNORE]", "status": "ok"},
        {"direction": "in", "content": "  ", "status": "ok"},
        {"direction": "out", "content": "x" * 1000, "status": "ok"},
    ]
    lines = ws.recent_lines(rows, "alex")
    assert lines[0] == "- alex: hi"
    assert len(lines) == 2
    assert lines[1].startswith("- you: xxx") and lines[1].endswith("...")
    assert len(lines[1]) < 320


def _ctx(channel: Channel, *, fresh: bool = False) -> TurnCtx:
    return TurnCtx(
        channel=channel,
        conversation=_conversation(),
        text="hello",
        person_id="alex",
        turn_id="t1",
        fresh_session=fresh,
    )


async def test_the_provider_ignores_every_other_channel() -> None:
    block = ws.WorkspaceBlock(WsRepo(_ws()))
    assert await block(_ctx(Channel(id="cli:alex", channel_type="cli"))) is None


async def test_the_provider_marks_unseen_items_after_the_turn() -> None:
    repo = WsRepo(_ws(), [_item(ref="a", seen=False), _item(ref="b")])
    block = ws.WorkspaceBlock(repo)
    ctx = _ctx(_channel())

    text = await block(ctx)

    assert "New since your last turn" in text
    assert repo.seen == []  # not yet: the turn has not run
    for settle in ctx.after_turn:
        await settle()
    assert repo.seen == ["i-a"]


async def test_the_provider_adds_the_last_rows_only_for_a_fresh_session() -> None:
    repo = WsRepo(_ws())
    repo.tail = [{"direction": "in", "content": "earlier talk", "status": "ok"}]
    block = ws.WorkspaceBlock(repo)

    assert "earlier talk" not in await block(_ctx(_channel(), fresh=False))
    assert "- alex: earlier talk" in await block(_ctx(_channel(), fresh=True))


async def test_a_section_adds_text_and_a_failing_section_is_skipped() -> None:
    block = ws.WorkspaceBlock(WsRepo(_ws()))

    async def steps(_workspace):
        return "Next steps: write the permit"

    async def broken(_workspace):
        raise RuntimeError("down")

    block.add_section(steps)
    block.add_section(broken)
    assert "Next steps: write the permit" in await block(_ctx(_channel()))


async def test_a_turn_gets_the_block_and_a_fresh_flag(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    prompts: list[str] = []

    class Client(FakeSDKClient):
        async def query(self, prompt):
            prompts.append(prompt)

    from joshua_core.engine import agent

    monkeypatch.setattr(agent, "ClaudeSDKClient", Client)
    repo = WsRepo(_ws(goal="Go solar"))
    repo.tail = [{"direction": "in", "content": "before the cap", "status": "ok"}]
    manager = make_manager(repo, tmp_path)
    manager.add_context_provider(ws.WorkspaceBlock(repo))

    await manager.run_turn(_channel(), _conversation(), "next step?")
    await manager.run_turn(_channel(), _conversation("sess-9"), "and then?")

    assert "[Workspace: Solar install (solar)]" in prompts[0]
    assert "before the cap" in prompts[0]  # a first turn has no history
    assert "[Workspace: Solar install (solar)]" in prompts[1]
    assert "before the cap" not in prompts[1]  # the session holds it now


# --- the context cap -------------------------------------------------------------------


class _Assistant:
    parent_tool_use_id = None
    error = None
    content: list = []

    def __init__(self, usage):
        self.usage = usage


_Assistant.__name__ = "AssistantMessage"


def _client_with_context(tokens: int):
    class Client(FakeSDKClient):
        async def receive_response(self):
            yield _Assistant(
                {"input_tokens": 10, "cache_read_input_tokens": tokens, "output_tokens": 5}
            )
            async for msg in super().receive_response():
                yield msg

    return Client


async def test_a_turn_over_the_cap_clears_the_sdk_session(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    from joshua_core.engine import agent

    monkeypatch.setattr(agent, "ClaudeSDKClient", _client_with_context(6000))
    repo = WsRepo(_ws())
    manager = make_manager(repo, tmp_path)

    await manager.run_turn(_channel(), _conversation(), "big turn")

    assert repo.cleared == ["c-ws"]
    mc = manager._pool["c-ws"]
    assert mc.replace_pending and mc.conversation.sdk_session_id is None


async def test_a_turn_under_the_cap_keeps_the_session(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    from joshua_core.engine import agent

    monkeypatch.setattr(agent, "ClaudeSDKClient", _client_with_context(100))
    repo = WsRepo(_ws())
    await make_manager(repo, tmp_path).run_turn(_channel(), _conversation(), "small turn")
    assert repo.cleared == []


async def test_the_cap_never_touches_a_normal_chat(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    from joshua_core.engine import agent

    monkeypatch.setattr(agent, "ClaudeSDKClient", _client_with_context(900_000))
    repo = WsRepo(_ws())
    channel = Channel(id="cli:alex", channel_type="cli")
    await make_manager(repo, tmp_path).run_turn(channel, _conversation(), "big turn")
    assert repo.cleared == []


# --- RAG ---------------------------------------------------------------------------------


def test_exclude_paths_map_documents_and_wiki_pages() -> None:
    items = [
        _item(kind="document", ref="solar-quote"),
        _item(kind="document", ref="documents/alex/plan-b.md", title="B"),
        _item(kind="document", ref="x", url="https://v.example/documents/alex/from-url?v=1"),
        _item(kind="wiki", ref="homelab/solar.md"),
        _item(kind="wiki", ref="wiki/garden"),
        _item(kind="link", ref="https://example.org/a"),
    ]
    assert ws.exclude_paths(_ws(), items) == {
        "documents/alex/solar-quote.md",
        "documents/alex/plan-b.md",
        "documents/alex/x.md",
        "documents/alex/from-url.md",
        "wiki/homelab/solar.md",
        "wiki/garden.md",
    }


def _chunk(path: str) -> KbChunk:
    return KbChunk(
        person_id="alex",
        source="files",
        kind="wiki",
        path=path,
        title=path,
        heading="",
        text=f"text of {path}",
        similarity=0.95,
    )


class _Store:
    def __init__(self, rows):
        self.rows = rows

    async def kb_search(self, *a, **k):
        return list(self.rows)


class _AuditRepo:
    def __init__(self):
        self.events = []

    async def add_kb_event(self, **kw):
        self.events.append(kw)


async def test_kb_context_skips_the_excluded_paths(monkeypatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model: [0.1] * 384)
    store = _Store([_chunk("wiki/in-workspace.md"), _chunk("wiki/elsewhere.md")])
    ctx = SimpleNamespace(
        text="what is the plan for the roof",
        person_id="alex",
        turn_id="t",
        conversation=SimpleNamespace(id="c", person_id="alex"),
        role="member",
    )
    kwargs = dict(
        store=store,
        repo=_AuditRepo(),
        embed_model="m",
        full_sim=0.7,
        hint_sim=0.5,
        top_k=3,
        max_chars=500,
        recency_bonus=0.0,
        recency_half_life_days=30,
        per_doc_cap=2,
    )
    plain = await kb_context(ctx, **kwargs)
    skipped = await kb_context(ctx, exclude_paths={"wiki/in-workspace.md"}, **kwargs)
    assert "in-workspace" in plain and "elsewhere" in plain
    assert "in-workspace" not in skipped and "elsewhere" in skipped


async def test_rag_exclude_paths_reads_only_a_workspace_channel() -> None:
    repo = WsRepo(_ws(), [_item(kind="wiki", ref="solar")])
    on = await ws.rag_exclude_paths(repo, _ctx(_channel()))
    off = await ws.rag_exclude_paths(repo, _ctx(Channel(id="cli:alex", channel_type="cli")))
    assert on == {"wiki/solar.md"} and off == set()
    assert await ws.rag_exclude_paths(repo, SimpleNamespace()) == set()


# --- the delivery gate ----------------------------------------------------------------------


def test_the_delivery_gate_drops_a_workspace_target_only() -> None:
    assert workspace_delivery_target(channel_type="workspace", target=f"workspace:{WS_ID}") is None
    assert workspace_delivery_target(channel_type="telegram", target="telegram:1") == "telegram:1"
    assert workspace_delivery_target(channel_type="cli", target="cli:alex") == "cli:alex"


# --- the builtin tools ---------------------------------------------------------------------------


def _deps(repo: WsRepo, channel: Channel | None = None) -> ToolDeps:
    return ToolDeps(
        repo=repo,
        conversation=_conversation(),
        channel=channel or _channel(),
        tz="UTC",
        person_id="alex",
    )


def _text(reply: dict) -> str:
    return reply["content"][0]["text"]


def _names(server: dict) -> set[str]:
    return {t.name for t in server["tools"]}


@pytest.fixture
def listed_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the builder return its tools, so a test reads their names."""
    monkeypatch.setattr(
        ws_tools, "create_sdk_mcp_server", lambda *, name, version, tools: {"tools": tools}
    )


async def test_add_in_a_workspace_defaults_to_the_current_one() -> None:
    repo = WsRepo(_ws())
    reply = await ws_tools.do_add_to_workspace(
        _deps(repo), kind="link", ref="https://example.org", title="Quote site"
    )
    assert "Added link" in _text(reply)
    assert repo.added[0]["workspace_id"] == WS_ID
    assert repo.added[0]["seen"] is True


async def test_add_in_normal_chat_needs_a_workspace_and_marks_the_item_unseen() -> None:
    repo = WsRepo(_ws(), None)
    repo.others = [
        _ws(id="22222222-2222-2222-2222-222222222222", slug="turkey", title="Thanksgiving plans")
    ]
    normal = Channel(id="cli:alex", channel_type="cli")

    missing = await ws_tools.do_add_to_workspace(
        _deps(repo, normal), kind="link", ref="r", title="Recipe"
    )
    assert "Name the workspace" in _text(missing)
    assert repo.added == []

    by_title = await ws_tools.do_add_to_workspace(
        _deps(repo, normal), kind="link", ref="r", title="Recipe", workspace="thanksgiving PLANS"
    )
    assert "Thanksgiving plans" in _text(by_title)
    assert repo.added[0]["workspace_id"].startswith("2222")
    assert repo.added[0]["seen"] is False

    unknown = await ws_tools.do_add_to_workspace(
        _deps(repo, normal), kind="link", ref="r", title="R", workspace="nowhere"
    )
    assert "No such workspace" in _text(unknown) and "solar" in _text(unknown)


async def test_add_refuses_a_bad_kind_and_an_empty_ref() -> None:
    repo = WsRepo(_ws())
    assert "Unknown kind" in _text(
        await ws_tools.do_add_to_workspace(_deps(repo), kind="photo", ref="r", title="t")
    )
    assert "Give a ref" in _text(
        await ws_tools.do_add_to_workspace(_deps(repo), kind="link", ref=" ", title="t")
    )
    assert repo.added == []


async def test_update_notes_replaces_them_in_a_workspace_only() -> None:
    repo = WsRepo(_ws())
    assert "Notes updated" in _text(
        await ws_tools.do_update_workspace_notes(_deps(repo), notes=" a ")
    )
    assert repo.notes == ["a"]
    normal = Channel(id="cli:alex", channel_type="cli")
    assert "not a workspace" in _text(
        await ws_tools.do_update_workspace_notes(_deps(repo, normal), notes="x")
    )
    too_long = await ws_tools.do_update_workspace_notes(_deps(repo), notes="x" * 5000)
    assert "Keep them under" in _text(too_long)
    assert repo.notes == ["a"]


async def test_send_to_normal_chat_goes_to_the_main_conversation_with_a_link() -> None:
    repo = WsRepo(_ws())
    repo.main = Conversation(
        id="m", channel_id="telegram:1", person_id="alex", last_channel_id="imessage:+1"
    )
    sent: list[tuple[str, str]] = []

    async def deliver(target: str, text: str) -> bool:
        sent.append((target, text))
        return True

    settings = make_settings(mcp=MCP)
    reply = await ws_tools.do_send_to_normal_chat(
        _deps(repo), text="Done", settings=settings, deliver=deliver
    )
    assert "Sent" in _text(reply)
    assert sent == [
        ("imessage:+1", "Done\n\nSolar install: https://joshua.example.org/chat/w/solar")
    ]


async def test_send_to_normal_chat_falls_back_to_the_path_and_reports_failures() -> None:
    repo = WsRepo(_ws())
    settings = make_settings()  # no viewer_url
    assert ws_tools.chat_link(settings, _ws()) == "/chat/w/solar"

    async def no(target: str, text: str) -> bool:
        return False

    async def yes(target: str, text: str) -> bool:
        return True

    kwargs = dict(text="Done", settings=settings)
    assert "no normal chat" in _text(
        await ws_tools.do_send_to_normal_chat(_deps(repo), deliver=yes, **kwargs)
    )
    repo.main = Conversation(id="m", channel_id="telegram:1", person_id="alex")
    assert "not delivered" in _text(
        await ws_tools.do_send_to_normal_chat(_deps(repo), deliver=no, **kwargs)
    )
    assert "not available" in _text(
        await ws_tools.do_send_to_normal_chat(_deps(repo), deliver=None, **kwargs)
    )
    assert "Give the text" in _text(
        await ws_tools.do_send_to_normal_chat(_deps(repo), text=" ", settings=settings, deliver=yes)
    )
    normal = Channel(id="cli:alex", channel_type="cli")
    assert "not a workspace" in _text(
        await ws_tools.do_send_to_normal_chat(_deps(repo, normal), deliver=yes, **kwargs)
    )


def test_the_server_has_three_tools_in_a_workspace_and_one_in_normal_chat(
    listed_tools: None,
) -> None:
    settings = make_settings()
    repo = WsRepo(_ws())
    in_ws = ws_tools.build_workspace_server(_deps(repo), settings)
    normal = ws_tools.build_workspace_server(
        _deps(repo, Channel(id="cli:alex", channel_type="cli")), settings
    )
    assert _names(in_ws) == {"add_to_workspace", "update_workspace_notes", "send_to_normal_chat"}
    assert _names(normal) == {"add_to_workspace"}


def test_register_wires_the_builtin_for_every_job() -> None:
    manager = SimpleNamespace(registered={})
    manager.register_builtin = lambda name, factory, **kw: manager.registered.update({name: kw})
    ws_tools.register(manager, make_settings(), None)
    assert manager.registered == {"workspace": {}}


# --- the phase 1 review fixes -----------------------------------------------------------


async def test_a_provider_that_raises_does_not_fail_the_turn(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    prompts: list[str] = []

    class Client(FakeSDKClient):
        async def query(self, prompt):
            prompts.append(prompt)

    from joshua_core.engine import agent

    monkeypatch.setattr(agent, "ClaudeSDKClient", Client)
    manager = make_manager(WsRepo(_ws()), tmp_path)

    async def broken(ctx):
        raise RuntimeError("db down")

    async def good(ctx):
        return "NOTE-FROM-GOOD"

    manager.add_context_provider(broken)
    manager.add_context_provider(good)
    result = await manager.run_turn(_channel(), _conversation(), "hello")

    assert not result.is_error
    assert "NOTE-FROM-GOOD" in prompts[0]


async def test_rag_exclusions_fall_back_to_none_on_an_error() -> None:
    class Broken(WsRepo):
        async def list_workspace_items(self, workspace_id, *, unseen_only=False):
            raise RuntimeError("db down")

    assert await ws.rag_exclude_paths(Broken(_ws()), _ctx(_channel())) == set()


async def test_without_the_context_size_the_session_is_kept(tmp_path, monkeypatch):
    install_fake_sdk(monkeypatch)
    from joshua_core.engine import agent

    # Summed over calls, this would be over the cap: it counts cache reads again.
    monkeypatch.setattr(agent, "ClaudeSDKClient", _client_with_context(6000))
    monkeypatch.setattr(agent, "_context_tokens", lambda usage: None)
    repo = WsRepo(_ws())
    await make_manager(repo, tmp_path).run_turn(_channel(), _conversation(), "big turn")
    assert repo.cleared == []


def _group_channel() -> Channel:
    return Channel(id="telegram:-100", channel_type="telegram", session_mode="shared")


def test_a_group_chat_gets_no_workspace_server(listed_tools: None) -> None:
    deps = ToolDeps(
        repo=WsRepo(_ws()),
        conversation=Conversation(id="g", channel_id="telegram:-100", person_id=None),
        channel=_group_channel(),
        tz="UTC",
        person_id="alex",
    )
    assert ws_tools.build_workspace_server(deps, make_settings()) is None


async def test_a_group_chat_error_names_no_workspace() -> None:
    repo = WsRepo(_ws())
    deps = ToolDeps(
        repo=repo,
        conversation=Conversation(id="g", channel_id="telegram:-100", person_id=None),
        channel=_group_channel(),
        tz="UTC",
        person_id="alex",
    )
    reply = await ws_tools.do_add_to_workspace(
        deps, kind="link", ref="r", title="R", workspace="nowhere"
    )
    assert _text(reply) == "No such workspace."


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,x",
        "//evil.example/a",
        "file:///etc/passwd",
        "ftp://x/y",
        "http://",
    ],
)
async def test_add_refuses_an_unsafe_url(url: str) -> None:
    repo = WsRepo(_ws())
    reply = await ws_tools.do_add_to_workspace(
        _deps(repo), kind="link", ref="r", title="t", url=url
    )
    assert "url was not added" in _text(reply)
    assert repo.added == []


async def test_add_accepts_http_https_and_a_relative_path() -> None:
    repo = WsRepo(_ws())
    for url in ("https://example.org/a", "http://example.org", "/documents/alex/x"):
        await ws_tools.do_add_to_workspace(_deps(repo), kind="link", ref=url, title="t", url=url)
    assert [a["url"] for a in repo.added] == [
        "https://example.org/a",
        "http://example.org",
        "/documents/alex/x",
    ]


async def test_a_scheduled_or_event_turn_in_a_workspace_gets_the_unattended_note(
    tmp_path, monkeypatch
):
    install_fake_sdk(monkeypatch)
    prompts: list[str] = []

    class Client(FakeSDKClient):
        async def query(self, prompt):
            prompts.append(prompt)

    from joshua_core.engine import agent

    monkeypatch.setattr(agent, "ClaudeSDKClient", Client)
    manager = make_manager(WsRepo(_ws()), tmp_path)
    await manager.run_turn(_channel(), _conversation(), "water the plants", direction="task")
    await manager.run_turn(_channel(), _conversation(), "build done", direction="inject")
    await manager.run_turn(_channel(), _conversation(), "hi")
    for prompt in prompts[:2]:
        assert "Nothing you write here reaches the person" in prompt
        assert "send_to_normal_chat" in prompt
    assert "Nothing you write here reaches" not in prompts[2]


async def test_the_scheduling_tool_notes_the_workspace_rule(monkeypatch) -> None:
    from joshua_core.engine.tools import scheduling

    seen: list[str] = []
    monkeypatch.setattr(
        scheduling,
        "tool",
        lambda name, desc, schema: (
            (seen.append(desc) or (lambda f: f)) if name == "schedule_task" else (lambda f: f)
        ),
    )
    monkeypatch.setattr(scheduling, "create_sdk_mcp_server", lambda **kw: None)
    scheduling.build_scheduling_server(_deps(WsRepo(_ws())))
    scheduling.build_scheduling_server(
        _deps(WsRepo(_ws()), Channel(id="cli:alex", channel_type="cli"))
    )
    assert "send_to_normal_chat" in seen[0]
    assert "send_to_normal_chat" not in seen[1]
