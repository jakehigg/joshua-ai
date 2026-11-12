"""In-process ``workspace`` tool server: collect things into a workspace, keep
its notes, and send the person a message in normal chat.

``add_to_workspace`` is in every chat. Inside a workspace thread it adds to the
current workspace unless the call names another. In a normal chat it must name
one of the person's workspaces, by slug or by title, and the item shows in the
block of that workspace as new. ``update_workspace_notes`` and
``send_to_normal_chat`` are in a workspace thread only: the server builder reads
``deps.channel.channel_type``.

Every tool acts for ``deps.person_id``. A workspace of another person is never
found.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.workspace import workspace_id_of
from joshua_core.store.models import WORKSPACE_ITEM_KINDS, Workspace
from joshua_core.store.repo import WORKSPACE_CHANNEL_TYPE

logger = get_logger("tools.workspace")

SERVER_NAME = "workspace"

# The notes are in every turn of the workspace, so they stay short.
NOTES_MAX_CHARS = 4000

Deliver = Callable[[str, str], Awaitable[bool]]


def _reply(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _in_workspace(deps: ToolDeps) -> bool:
    return deps.channel.channel_type == WORKSPACE_CHANNEL_TYPE


async def _current(deps: ToolDeps) -> Workspace | None:
    workspace_id = workspace_id_of(deps.channel.id)
    found = await deps.repo.get_workspace(workspace_id) if workspace_id else None
    return found if found is not None and found.owner == deps.person_id else None


async def _named(deps: ToolDeps, name: str) -> Workspace | None:
    """A workspace of the person by slug, id, or title (not case-sensitive)."""
    if not deps.person_id:
        return None
    found = await deps.repo.find_workspace(deps.person_id, name)
    if found is not None:
        return found
    wanted = name.strip().casefold()
    for workspace in await deps.repo.list_workspaces(deps.person_id):
        if workspace.title.strip().casefold() == wanted:
            return workspace
    return None


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
    target: Workspace | None
    if workspace and workspace.strip():
        target = await _named(deps, workspace)
    elif _in_workspace(deps):
        target = await _current(deps)
    else:
        return _reply("Name the workspace: its slug or its title.")
    if target is None:
        names = ", ".join(w.slug for w in await _active(deps)) or "none"
        return _reply(f"No such workspace. Your workspaces: {names}.")
    here = _in_workspace(deps) and target.channel_id == deps.channel.id
    await deps.repo.add_workspace_item(
        target.id,
        kind,
        ref,
        title or ref,
        url=(url or "").strip() or None,
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
    or the path alone when ``workspaces.viewer_url`` is not set."""
    path = f"/chat/w/{workspace.slug}"
    base = settings.workspaces.viewer_url
    return f"{base.rstrip('/')}{path}" if base else path


async def do_send_to_normal_chat(
    deps: ToolDeps, *, text: str, settings: JoshuaConfig, deliver: Deliver | None
) -> dict[str, Any]:
    """Send ``text`` and a link to this workspace to the person's main chat."""
    if not _in_workspace(deps):
        return _reply("This chat is not a workspace.")
    text = (text or "").strip()
    if not text:
        return _reply("Give the text to send.")
    if deliver is None or not deps.person_id:
        return _reply("Sending to normal chat is not available here.")
    workspace = await _current(deps)
    if workspace is None:
        return _reply("The workspace of this chat was not found.")
    main = await deps.repo.main_conversation(deps.person_id)
    if main is None:
        return _reply("The person has no normal chat to send to.")
    target = main.last_channel_id or main.channel_id
    message = f"{text}\n\n{workspace.title}: {chat_link(settings, workspace)}"
    if not await deliver(target, message):
        return _reply("The message was not delivered. Tell the person in this chat.")
    return _reply("Sent to the person's normal chat.")


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

_SEND_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "The message."}},
    "required": ["text"],
}


def build_workspace_server(
    deps: ToolDeps, settings: JoshuaConfig, deliver: Deliver | None = None
) -> Any:
    """Build the ``workspace`` MCP server for one conversation.

    A workspace thread gets all three tools. Any other chat gets
    ``add_to_workspace`` only.
    """

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

    tools = [add_to_workspace]
    if _in_workspace(deps):
        tools += [update_workspace_notes, send_to_normal_chat]
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)


def register(manager: Any, settings: JoshuaConfig, deliverer: Any = None) -> None:
    """Wire the ``workspace`` builtin into the conversation manager.

    ``deliverer`` is core's ``Deliverer``. Without one, ``send_to_normal_chat``
    says it is not available.
    """
    deliver: Deliver | None = deliverer.deliver if deliverer is not None else None
    manager.register_builtin(
        SERVER_NAME, lambda deps: build_workspace_server(deps, settings, deliver)
    )
