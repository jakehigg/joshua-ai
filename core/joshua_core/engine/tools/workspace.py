"""In-process ``workspace`` tool server: collect things into a workspace, keep
its notes, and send the person a message in normal chat.

``add_to_workspace`` is in every chat. Inside a workspace thread it adds to the
current workspace unless the call names another. In a normal chat it must name
one of the person's workspaces, by slug or by title, and the item shows in the
block of that workspace as new. ``update_workspace_notes`` and
``send_to_normal_chat`` are in a workspace thread only: the server builder reads
``deps.channel.channel_type``.

A direct chat that is not a workspace also gets ``send_to_workspace`` (a row and
items in the thread, no turn), ``tell_workspace`` (the same, then one workspace
turn whose reply stays in the thread), ``list_workspaces`` and
``workspace_status`` (the block as text). A workspace name is a slug or a title,
matched with no regard to case among the workspaces of the person.

Every tool acts for ``deps.person_id``, the person who wrote the turn: the
workspaces they reach are their own and the shared ones they are in. A
workspace they do not reach is never found.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from joshua_shared.config import JoshuaConfig
from joshua_shared.item_url import check_item_url
from joshua_shared.log import get_logger

from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.workspace import workspace_id_of
from joshua_core.store.models import WORKSPACE_ITEM_KINDS, Workspace
from joshua_core.store.repo import SHARED_NAMESPACE, WORKSPACE_CHANNEL_TYPE

logger = get_logger("tools.workspace")

SERVER_NAME = "workspace"

# The notes are in every turn of the workspace, so they stay short.
NOTES_MAX_CHARS = 4000

Deliver = Callable[[str, str], Awaitable[bool]]
# Starts one turn in a workspace thread, in the background: (workspace, text).
RunTurn = Callable[[Workspace, str], Awaitable[None]]
# Renders the workspace block as text, with no side effect.
RenderBlock = Callable[[Workspace], Awaitable[str]]

GOAL_LINE_CHARS = 120
# send_to_workspace and tell_workspace cut a message at this length.
MESSAGE_MAX_CHARS = 4000
CHAT_PREFIX = "[From normal chat] "


def _reply(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _in_workspace(deps: ToolDeps) -> bool:
    return deps.channel.channel_type == WORKSPACE_CHANNEL_TYPE


async def _current(deps: ToolDeps) -> Workspace | None:
    """The workspace of this chat, for the person of the turn. A turn with no
    person, a job event or a scheduled task, still gets it: the thread is the
    workspace. A person who does not reach the workspace gets None."""
    workspace_id = workspace_id_of(deps.channel.id)
    found = await deps.repo.get_workspace(workspace_id) if workspace_id else None
    if found is None:
        return None
    return found if deps.person_id is None or deps.person_id in found.people else None


async def _named(deps: ToolDeps, name: str) -> Workspace | None:
    """A workspace the person reaches, by slug, id, or title (not
    case-sensitive). An ambiguous title gives None."""
    found, _ = await _match(deps, name)
    return found


async def _match(deps: ToolDeps, name: str) -> tuple[Workspace | None, list[Workspace]]:
    """``(workspace, candidates)``. ``workspace`` is the one match among the
    workspaces the person reaches, their own and the shared ones they are in.
    With no match it is None and ``candidates`` is empty. With an ambiguous name
    it is None and ``candidates`` lists the workspaces that match."""
    if not deps.person_id or not (name or "").strip():
        return None, []
    wanted = name.strip().casefold()
    reached = await deps.repo.list_workspaces(deps.person_id)
    exact = [w for w in reached if wanted in (w.slug.casefold(), w.id.casefold())]
    if len(exact) == 1:
        return exact[0], []
    by_title = [w for w in reached if w.title.strip().casefold() == wanted]
    if len(by_title) == 1:
        return by_title[0], []
    if by_title:
        return None, by_title
    return None, []


async def do_add_to_workspace(
    deps: ToolDeps,
    *,
    kind: str,
    ref: str,
    title: str,
    url: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Add an item to a workspace, or update the item with the same kind and ref."""
    kind, ref, title = (kind or "").strip(), (ref or "").strip(), (title or "").strip()
    if kind not in WORKSPACE_ITEM_KINDS:
        return _reply(f"Unknown kind '{kind}'. Use one of: {', '.join(WORKSPACE_ITEM_KINDS)}.")
    if not ref:
        return _reply("Give a ref: the id, slug, path, or address of the item.")
    try:
        address = check_item_url(url)
    except ValueError as exc:
        return _reply(f"The url was not added: {exc}.")
    target: Workspace | None
    if workspace and workspace.strip():
        target = await _named(deps, workspace)
    elif _in_workspace(deps):
        target = await _current(deps)
    else:
        return _reply("Name the workspace: its slug or its title.")
    if target is None:
        # Only a direct chat may see the names of the workspaces: a group chat
        # shows to other people.
        if deps.channel.session_mode != "per_person":
            return _reply("No such workspace.")
        names = ", ".join(w.slug for w in await _active(deps)) or "none"
        return _reply(f"No such workspace. Your workspaces: {names}.")
    here = _in_workspace(deps) and target.channel_id == deps.channel.id
    await deps.repo.add_workspace_item(
        target.id,
        kind,
        ref,
        title or ref,
        url=address,
        added_by="agent",
        # An item sent in from another chat is new to the workspace.
        seen=here,
    )
    return _reply(f"Added {kind} '{title or ref}' to workspace '{target.title}'.")


async def _active(deps: ToolDeps) -> list[Workspace]:
    if not deps.person_id:
        return []
    return await deps.repo.list_workspaces(deps.person_id, "active")


async def do_update_workspace_notes(deps: ToolDeps, *, notes: str) -> dict[str, Any]:
    """Replace the notes of the current workspace."""
    if not _in_workspace(deps):
        return _reply("This chat is not a workspace.")
    notes = (notes or "").strip()
    if len(notes) > NOTES_MAX_CHARS:
        return _reply(
            f"The notes are {len(notes)} characters. Keep them under {NOTES_MAX_CHARS}: "
            "cut what is old, and put long material in a document."
        )
    workspace = await _current(deps)
    if workspace is None:
        return _reply("The workspace of this chat was not found.")
    await deps.repo.update_workspace(workspace.id, notes=notes)
    return _reply("Notes updated.")


def chat_link(settings: JoshuaConfig, workspace: Workspace) -> str:
    """The address of a workspace in the viewer: the public address and the path,
    or the path alone when ``workspaces.viewer_url`` is not set. The path names
    the namespace of the workspace: ``shared`` when it is shared, its owner when
    it is not."""
    namespace = SHARED_NAMESPACE if workspace.shared else workspace.owner
    path = f"/chat/w/{namespace}/{workspace.slug}"
    base = settings.workspaces.viewer_url
    return f"{base.rstrip('/')}{path}" if base else path


async def do_send_to_normal_chat(
    deps: ToolDeps, *, text: str, settings: JoshuaConfig, deliver: Deliver | None
) -> dict[str, Any]:
    """Send ``text`` and a link to this workspace to the main chat of one
    person: the one who wrote the turn. A turn with no person, a job event or a
    scheduled task, sends to the owner, so the message reaches somebody. There
    is no fan-out: to tell a whole shared workspace something, the agent writes
    in the thread."""
    if not _in_workspace(deps):
        return _reply("This chat is not a workspace.")
    text = (text or "").strip()
    if not text:
        return _reply("Give the text to send.")
    if deliver is None:
        return _reply("Sending to normal chat is not available here.")
    workspace = await _current(deps)
    if workspace is None:
        return _reply("The workspace of this chat was not found.")
    main = await deps.repo.main_conversation(deps.person_id or workspace.owner)
    if main is None:
        return _reply("The person has no normal chat to send to.")
    target = main.last_channel_id or main.channel_id
    message = f"{text}\n\n{workspace.title}: {chat_link(settings, workspace)}"
    if not await deliver(target, message):
        return _reply("The message was not delivered. Tell the person in this chat.")
    return _reply("Sent to the person's normal chat.")


def _normal_chat(deps: ToolDeps) -> bool:
    """A direct chat that is not a workspace: the home of the four tools."""
    return not _in_workspace(deps) and deps.channel.session_mode == "per_person"


def _one_line(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


async def _resolve(deps: ToolDeps, name: str) -> tuple[Workspace | None, str]:
    """The workspace that ``name`` means, or None and the answer to give."""
    if not (name or "").strip():
        return None, f"Name the workspace: its slug or its title.{await _known(deps)}"
    found, candidates = await _match(deps, name)
    if found is not None:
        return found, ""
    if candidates:
        slugs = ", ".join(f"{w.slug} ({w.title})" for w in candidates)
        return None, f"More than one workspace has that title. Use a slug: {slugs}."
    return None, f"No such workspace.{await _known(deps)}"


async def _known(deps: ToolDeps) -> str:
    names = ", ".join(f"{w.slug}" for w in await _active(deps)) or "none"
    return f" Your workspaces: {names}."


async def _items_in(deps: ToolDeps, workspace: Workspace, items: Any) -> tuple[int, list[str]]:
    """Add the items of ``send_to_workspace`` as unseen. Returns the number
    added and the problems found."""
    added, problems = 0, []
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict):
            problems.append("an item that is not an object")
            continue
        kind = (raw.get("kind") or "link").strip()
        ref, title = (raw.get("ref") or "").strip(), (raw.get("title") or "").strip()
        if kind not in WORKSPACE_ITEM_KINDS or not ref:
            problems.append(f"item '{title or ref or kind}' needs a known kind and a ref")
            continue
        try:
            address = check_item_url(raw.get("url") or None)
        except ValueError as exc:
            problems.append(f"item '{title or ref}': {exc}")
            continue
        await deps.repo.add_workspace_item(
            workspace.id, kind, ref, title or ref, url=address, added_by="agent", seen=False
        )
        added += 1
    return added, problems


def _message(text: str) -> str:
    return (text or "").strip()[:MESSAGE_MAX_CHARS]


async def do_send_to_workspace(
    deps: ToolDeps, *, workspace: str, text: str, items: Any = None
) -> dict[str, Any]:
    """Write ``text`` into the thread of a workspace as a row from normal chat,
    and add the items as unseen. No turn runs: the block of the workspace shows
    both on its next turn, once."""
    if not _normal_chat(deps):
        return _reply("This tool is for normal chat.")
    text = _message(text)
    if not text:
        return _reply("Give the text to send.")
    target, problem = await _resolve(deps, workspace)
    if target is None:
        return _reply(problem)
    if target.status == "archived":
        return _reply(f"Workspace '{target.title}' is archived.")
    conversation = await deps.repo.workspace_conversation(target)
    added, problems = await _items_in(deps, target, items)
    await deps.repo.add_chat_row(conversation.id, text)
    note = f" with {added} item(s)" if added else ""
    tail = f" Not added: {'; '.join(problems)}." if problems else ""
    return _reply(f"Recorded in workspace '{target.title}'{note}. It shows on its next turn.{tail}")


async def do_tell_workspace(
    deps: ToolDeps, *, workspace: str, text: str, run: RunTurn | None
) -> dict[str, Any]:
    """Run one turn in the workspace thread with ``text``. The reply stays in the
    thread. The turn runs in the background."""
    if not _normal_chat(deps):
        return _reply("This tool is for normal chat.")
    text = _message(text)
    if not text:
        return _reply("Give the text to send.")
    if run is None:
        return _reply("Telling a workspace is not available here.")
    target, problem = await _resolve(deps, workspace)
    if target is None:
        return _reply(problem)
    if target.status != "active":
        return _reply(f"Workspace '{target.title}' is {target.status}. Resume it first.")
    await run(target, CHAT_PREFIX + text)
    return _reply(
        f"Told workspace '{target.title}'. It is working on it, and its reply "
        "stays in the workspace thread."
    )


async def do_list_workspaces(deps: ToolDeps) -> dict[str, Any]:
    """The workspaces of the person: title, slug, status, and the goal."""
    if not _normal_chat(deps):
        return _reply("This tool is for normal chat.")
    owned = await deps.repo.list_workspaces(deps.person_id) if deps.person_id else []
    if not owned:
        return _reply("The person has no workspaces.")
    lines = []
    for w in owned:
        goal = f" - {_one_line(w.goal, GOAL_LINE_CHARS)}" if w.goal else ""
        lines.append(f"- {w.title} ({w.slug}, {w.status}){goal}")
    return _reply("\n".join(lines))


async def do_workspace_status(
    deps: ToolDeps, *, workspace: str, render: RenderBlock | None
) -> dict[str, Any]:
    """The workspace block as text, never the thread."""
    if not _normal_chat(deps):
        return _reply("This tool is for normal chat.")
    if render is None:
        return _reply("The workspace status is not available here.")
    target, problem = await _resolve(deps, workspace)
    if target is None:
        return _reply(problem)
    return _reply(await render(target))


_ADD_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": list(WORKSPACE_ITEM_KINDS),
            "description": "What the item is.",
        },
        "ref": {
            "type": "string",
            "description": "The id, slug, path, or address of the item. It identifies the item.",
        },
        "title": {"type": "string", "description": "A short title the person reads."},
        "url": {"type": "string", "description": "An address that opens the item."},
        "workspace": {
            "type": "string",
            "description": (
                "The slug or the title of the workspace. Optional in a workspace chat, "
                "where it defaults to this workspace. Required in a normal chat."
            ),
        },
    },
    "required": ["kind", "ref", "title"],
}

_NOTES_SCHEMA = {
    "type": "object",
    "properties": {"notes": {"type": "string", "description": "The full new notes."}},
    "required": ["notes"],
}

_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(WORKSPACE_ITEM_KINDS)},
        "ref": {"type": "string", "description": "The id, slug, path, or address of the item."},
        "title": {"type": "string"},
        "url": {"type": "string"},
    },
    "required": ["ref"],
}

_WORKSPACE_ARG = {
    "type": "string",
    "description": "The slug or the title of one of the person's workspaces.",
}

_SEND_WS_SCHEMA = {
    "type": "object",
    "properties": {
        "workspace": _WORKSPACE_ARG,
        "text": {"type": "string", "description": "What to record in the workspace."},
        "items": {
            "type": "array",
            "items": _ITEM_SCHEMA,
            "description": "Links, documents, or other items to add with it.",
        },
    },
    "required": ["workspace", "text"],
}

_TELL_SCHEMA = {
    "type": "object",
    "properties": {"workspace": _WORKSPACE_ARG, "text": {"type": "string"}},
    "required": ["workspace", "text"],
}

_STATUS_SCHEMA = {
    "type": "object",
    "properties": {"workspace": _WORKSPACE_ARG},
    "required": ["workspace"],
}

_NO_ARGS_SCHEMA = {"type": "object", "properties": {}}

_SEND_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "The message."}},
    "required": ["text"],
}


def build_workspace_server(
    deps: ToolDeps,
    settings: JoshuaConfig,
    deliver: Deliver | None = None,
    run: RunTurn | None = None,
    render: RenderBlock | None = None,
) -> Any | None:
    """Build the ``workspace`` MCP server for one conversation.

    A workspace thread gets three tools. A direct chat gets ``add_to_workspace``
    and the four tools of normal chat. A shared (group) chat gets no server: the
    workspaces of one person do not belong in a chat that others read.
    """
    if not _in_workspace(deps) and deps.channel.session_mode != "per_person":
        return None

    @tool(
        "add_to_workspace",
        "Add an item to a workspace: a document, plan, wiki page, link, or anything made "
        "or found outside Joshua. Adding the same kind and ref again updates the item. "
        "In a workspace chat the workspace is this one unless you name another.",
        _ADD_SCHEMA,
    )
    async def add_to_workspace(args: dict[str, Any]) -> dict[str, Any]:
        return await do_add_to_workspace(
            deps,
            kind=args.get("kind", ""),
            ref=args.get("ref", ""),
            title=args.get("title", ""),
            url=args.get("url") or None,
            workspace=args.get("workspace") or None,
        )

    @tool(
        "update_workspace_notes",
        "Replace the notes of this workspace. The notes are standing facts and rules, and "
        "they ride in every turn: keep them short and current, and drop what is old.",
        _NOTES_SCHEMA,
    )
    async def update_workspace_notes(args: dict[str, Any]) -> dict[str, Any]:
        return await do_update_workspace_notes(deps, notes=args.get("notes", ""))

    @tool(
        "send_to_normal_chat",
        "Send the person one message in their normal chat, with a link back to this "
        "workspace. Use it only when they asked for a message, or when the notify "
        "setting of this workspace allows it.",
        _SEND_SCHEMA,
    )
    async def send_to_normal_chat(args: dict[str, Any]) -> dict[str, Any]:
        return await do_send_to_normal_chat(
            deps, text=args.get("text", ""), settings=settings, deliver=deliver
        )

    @tool(
        "send_to_workspace",
        "Record something in one of the person's workspaces, without a turn: use it when "
        "what they said belongs to a workspace (a recipe for the dinner plans, a quote for "
        "the solar project). The workspace reads it on its next turn. Add links or "
        "documents with items. Say in one line that you did it.",
        _SEND_WS_SCHEMA,
    )
    async def send_to_workspace(args: dict[str, Any]) -> dict[str, Any]:
        return await do_send_to_workspace(
            deps,
            workspace=args.get("workspace", ""),
            text=args.get("text", ""),
            items=args.get("items"),
        )

    @tool(
        "tell_workspace",
        "Give a workspace a message and have it act on it now: it runs one turn in its own "
        "thread. Its reply stays in the workspace. Use send_to_workspace when no action is "
        "needed.",
        _TELL_SCHEMA,
    )
    async def tell_workspace(args: dict[str, Any]) -> dict[str, Any]:
        return await do_tell_workspace(
            deps, workspace=args.get("workspace", ""), text=args.get("text", ""), run=run
        )

    @tool(
        "list_workspaces",
        "List the person's workspaces: title, slug, status, and goal.",
        _NO_ARGS_SCHEMA,
    )
    async def list_workspaces(args: dict[str, Any]) -> dict[str, Any]:
        return await do_list_workspaces(deps)

    @tool(
        "workspace_status",
        "The state of one workspace: its goal, notes, items, and open work. It does not "
        "show the thread.",
        _STATUS_SCHEMA,
    )
    async def workspace_status(args: dict[str, Any]) -> dict[str, Any]:
        return await do_workspace_status(deps, workspace=args.get("workspace", ""), render=render)

    tools = [add_to_workspace]
    if _in_workspace(deps):
        tools += [update_workspace_notes, send_to_normal_chat]
    else:
        tools += [send_to_workspace, tell_workspace, list_workspaces, workspace_status]
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)


def register(manager: Any, settings: JoshuaConfig, deliverer: Any = None, repo: Any = None) -> None:
    """Wire the ``workspace`` builtin into the conversation manager.

    ``deliverer`` is core's ``Deliverer``. Without one, ``send_to_normal_chat``
    says it is not available. ``tell_workspace`` runs its turn through the
    manager, and ``workspace_status`` renders the block of ``manager``.
    """
    deliver: Deliver | None = deliverer.deliver if deliverer is not None else None
    repo = repo or getattr(manager, "_repo", None)
    background: set[asyncio.Task[None]] = set()

    async def run(workspace: Workspace, text: str) -> None:
        """Start one turn in the thread of ``workspace`` and do not wait for it.
        The turn is an event turn: the delivery gate keeps its reply in the
        transcript."""
        channel = await repo.get_channel(workspace.channel_id)
        if channel is None:
            return
        conversation = await repo.workspace_conversation(workspace)

        async def turn() -> None:
            try:
                await manager.run_turn(channel, conversation, text, direction="inject")
            except Exception as exc:  # noqa: BLE001 — a failed turn is in the thread already
                logger.warning(
                    {"message": "tell_workspace turn failed", "error": type(exc).__name__}
                )

        task = asyncio.create_task(turn())
        background.add(task)
        task.add_done_callback(background.discard)

    async def render(workspace: Workspace) -> str:
        block = getattr(manager, "workspace_block", None)
        if block is None:
            return "The workspace status is not available here."
        conversation = await repo.workspace_conversation(workspace)
        return await block.render(workspace, conversation.id, fresh=False, mark_seen=None)

    manager.register_builtin(
        SERVER_NAME,
        lambda deps: build_workspace_server(deps, settings, deliver, run, render),
    )
