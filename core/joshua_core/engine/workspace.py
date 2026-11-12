"""Workspaces in the engine: the session of a workspace thread, the block that
every turn of it carries, and the paths that RAG skips.

A workspace thread is a conversation of the channel ``workspace:<id>``. It is
the full Joshua. Only four things differ from a normal direct chat: the model,
the servers, one prompt file (``workspace.md``), and the workspace block. This
module holds the pure parts of those. ``ConversationManager`` calls them.

Nothing here runs for a channel of another type.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from typing import Any

from joshua_shared.inference import (
    MODEL_TIER_KEYS,
    TOOL_SEARCH_KEY,
    TOOL_SEARCH_ON,
    ResolvedInference,
)
from joshua_shared.log import get_logger

from joshua_core.store.models import Workspace, WorkspaceItem
from joshua_core.store.repo import WORKSPACE_CHANNEL_TYPE, Repo
from joshua_core.store.transcript import FAILED_STATUS, SILENT_REPLY

logger = get_logger("engine.workspace")

# The prompt file that a workspace thread adds to the dm profile.
WORKSPACE_PROMPT_FILES = ("workspace.md",)

# The block lists at most this many items, pinned first, then newest first.
BLOCK_MAX_ITEMS = 30
# A fresh session gets this many rows of the thread, each cut short.
BLOCK_RECENT_ROWS = 10
RECENT_ROW_CHARS = 300


def workspace_id_of(channel_id: str) -> str | None:
    """The workspace id of a channel id ``workspace:<id>``, or None."""
    prefix = f"{WORKSPACE_CHANNEL_TYPE}:"
    return channel_id[len(prefix) :] if channel_id.startswith(prefix) else None


def signature(workspace: Workspace) -> tuple[str | None, tuple[str, ...]]:
    """What a session is built from: the model and the servers. A change of
    either makes the manager rebuild the session on the next turn."""
    return (workspace.model, tuple(workspace.servers))


def picked_servers(gateway_names: Iterable[str], workspace: Workspace) -> list[str]:
    """The gateway servers of the session: the ones the person may use that
    the workspace picked, in config order."""
    picked = set(workspace.servers)
    return [name for name in gateway_names if name in picked]


def tool_search_for(picked: list[str], max_servers: int) -> bool:
    """Tool search for a workspace session.

    Core does not learn the tool counts of the servers. The rule is by server:
    off when ``max_servers`` or fewer servers are picked, on for more.
    """
    return len(picked) > max_servers


def with_workspace(
    resolved: ResolvedInference, workspace: Workspace, *, tool_search: bool
) -> ResolvedInference:
    """The inference of a workspace session.

    The model of the workspace replaces the model of the job. On another
    server, the tier variables hold the job model, so they take the new one.
    The server and the key stay those of the job. ``tool_search`` replaces the
    setting of the job.
    """
    env = dict(resolved.env)
    model = resolved.model
    if workspace.model:
        model = workspace.model
        for key in MODEL_TIER_KEYS:
            if env.get(key):
                env[key] = model
    if tool_search:
        env[TOOL_SEARCH_KEY] = TOOL_SEARCH_ON
    else:
        env[TOOL_SEARCH_KEY] = ""
    return replace(resolved, model=model, env=env, tool_search=tool_search)


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _item_line(item: WorkspaceItem) -> str:
    pin = "[pinned] " if item.pinned else ""
    ref = f" ({item.ref})" if item.ref and item.ref != item.title else ""
    return f"- {pin}{item.kind}: {item.title or item.ref}{ref}"


def recent_lines(rows: list[dict[str, Any]], person: str | None) -> list[str]:
    """One short line for each visible row of the thread, oldest first."""
    lines = []
    for row in rows:
        text = (row.get("content") or "").strip()
        if not text or text == SILENT_REPLY or row.get("status") == FAILED_STATUS:
            continue
        who = "you" if row.get("direction") == "out" else (person or "person")
        lines.append(f"- {who}: {_one_line(text, RECENT_ROW_CHARS)}")
    return lines


def render_block(
    workspace: Workspace,
    items: list[WorkspaceItem],
    *,
    unseen: list[WorkspaceItem] | None = None,
    recent: list[str] | None = None,
    extra: list[str] | None = None,
) -> str:
    """The workspace block as text.

    It holds the title, the goal, the notes, the items (``BLOCK_MAX_ITEMS``,
    pinned first, then newest), the items that came from normal chat and are
    not yet seen, and, for a fresh session, the last rows of the thread.
    ``extra`` holds the text of the sections that later phases add.
    """
    lines = [f"[Workspace: {workspace.title} ({workspace.slug})]"]
    if workspace.goal:
        lines.append(f"Goal: {workspace.goal}")
    lines.append(f"Notify: {workspace.notify}")
    if workspace.notes:
        lines += ["Notes:", workspace.notes]
    if items:
        lines.append(f"Items ({min(len(items), BLOCK_MAX_ITEMS)} of {len(items)}):")
        lines += [_item_line(i) for i in items[:BLOCK_MAX_ITEMS]]
    else:
        lines.append("Items: none yet.")
    if unseen:
        lines.append("New since your last turn, sent from normal chat:")
        lines += [_item_line(i) for i in unseen]
    lines += extra or []
    if recent:
        lines.append("The last rows of this thread, because the session starts fresh:")
        lines += recent
    return "\n".join(lines)


# A section that a later phase adds to the block: it gets the workspace and
# returns text, or None for nothing to say. Phase 4 adds ``next_steps`` of a
# linked tasks project, and the running jobs.
BlockSection = Callable[[Workspace], Awaitable[str | None]]


class WorkspaceBlock:
    """The context provider of a workspace thread.

    It acts only on a channel of type ``workspace``, and returns None for every
    other turn. ``add_section`` is the hook for the sections of later phases.
    """

    def __init__(self, repo: Repo) -> None:
        self._repo = repo
        self._sections: list[BlockSection] = []

    def add_section(self, section: BlockSection) -> None:
        self._sections.append(section)

    async def render(
        self, workspace: Workspace, conversation_id: str, *, fresh: bool, mark_seen: Any = None
    ) -> str:
        items = await self._repo.list_workspace_items(workspace.id)
        unseen = [i for i in items if not i.seen]
        recent: list[str] = []
        if fresh:
            rows = await self._repo.transcript_tail(conversation_id, BLOCK_RECENT_ROWS)
            recent = recent_lines(rows, workspace.owner)
        extra = []
        for section in self._sections:
            try:
                text = await section(workspace)
            except Exception as exc:  # noqa: BLE001 — a section never breaks a turn
                logger.warning({"message": "block section failed", "error": type(exc).__name__})
                continue
            if text:
                extra.append(text)
        if unseen and mark_seen is not None:
            mark_seen(unseen)
        return render_block(workspace, items, unseen=unseen, recent=recent, extra=extra)

    async def __call__(self, ctx: Any) -> str | None:
        if ctx.channel.channel_type != WORKSPACE_CHANNEL_TYPE:
            return None
        workspace_id = workspace_id_of(ctx.channel.id)
        workspace = await self._repo.get_workspace(workspace_id) if workspace_id else None
        if workspace is None:
            return None

        def mark_seen(unseen: list[WorkspaceItem]) -> None:
            ids = [i.id for i in unseen]

            async def done() -> None:
                await self._repo.mark_items_seen(ids)

            ctx.after_turn.append(done)

        return await self.render(
            workspace, ctx.conversation.id, fresh=ctx.fresh_session, mark_seen=mark_seen
        )


# --- RAG --------------------------------------------------------------------


def exclude_paths(workspace: Workspace, items: Iterable[WorkspaceItem]) -> set[str]:
    """The KB paths of the documents and wiki pages that are items of the
    workspace. The block holds them already, so RAG skips them.

    A document maps to ``documents/<owner>/<slug>.md`` and a wiki page to its
    path under ``wiki/``. The ref or the url may be the slug, the path, or an
    address that ends in either.
    """
    paths: set[str] = set()
    for item in items:
        if item.kind not in ("document", "wiki"):
            continue
        for raw in (item.ref, item.url or ""):
            text = raw.split("?", 1)[0].split("#", 1)[0].strip().strip("/")
            if not text:
                continue
            if item.kind == "document":
                slug = text.rsplit("/", 1)[-1].removesuffix(".md")
                if slug:
                    paths.add(f"documents/{workspace.owner}/{slug}.md")
            else:
                path = text if text.startswith("wiki/") else f"wiki/{text}"
                paths.add(path if path.endswith(".md") else f"{path}.md")
    return paths


async def rag_exclude_paths(repo: Repo, ctx: Any) -> set[str]:
    """The paths that RAG skips on this turn: empty for a channel that is not a
    workspace."""
    channel = getattr(ctx, "channel", None)
    if getattr(channel, "channel_type", None) != WORKSPACE_CHANNEL_TYPE:
        return set()
    workspace_id = workspace_id_of(channel.id)
    workspace = await repo.get_workspace(workspace_id) if workspace_id else None
    if workspace is None:
        return set()
    return exclude_paths(workspace, await repo.list_workspace_items(workspace.id))
