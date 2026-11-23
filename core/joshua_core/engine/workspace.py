"""Workspaces in the engine: the session of a workspace thread, the block that
every turn of it carries, and the paths that RAG skips.

A workspace thread is a conversation of the channel ``workspace:<id>``. It is
the full Joshua. Only four things differ from a normal direct chat: the model,
the servers, the prompt files (``workspace.md``, ``workspace_shared.md`` for a
shared workspace, and the file of the writing style), and the workspace block.
This module holds the pure parts of those. ``ConversationManager`` calls them.

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
from joshua_core.store.notify import default_mode
from joshua_core.store.repo import (
    FROM_CHAT_SOURCE,
    SHARED_NAMESPACE,
    WORKSPACE_CHANNEL_TYPE,
    Repo,
)
from joshua_core.store.transcript import FAILED_STATUS, SILENT_REPLY

logger = get_logger("engine.workspace")

# The prompt file that a workspace thread adds to the dm profile.
WORKSPACE_PROMPT_FILES = ("workspace.md",)

# The block lists at most this many items, pinned first, then newest first.
BLOCK_MAX_ITEMS = 30
# A fresh session gets this many rows of the thread as short lines: the rows
# before the turns it carries whole.
BLOCK_RECENT_ROWS = 10
RECENT_ROW_CHARS = 300
# A row of the person is short, and its exact words matter: it is cut only
# past this length.
PERSON_ROW_CHARS = 2000
# A fresh session of a workspace gets this many turns whole, both sides of
# each: a turn is one message of the person and the reply to it.
CARRIED_TURNS = 3
# A carried message is cut past this length, because three whole turns with
# large tool results are long.
CARRIED_MESSAGE_CHARS = 4000
# What a cut carried message ends with.
CUT_NOTE = "\n[This message is cut here. Use `read_history` for the whole text.]"
# A fresh session of a workspace reads this many rows back: enough for the
# carried turns and the short lines of the rows before them.
FRESH_TAIL_ROWS = 30
# The heading of the carried turns.
RECENT_TURNS_HEAD = (
    "The recent conversation of this thread, word for word, oldest first. "
    "These turns are already answered. The message you are answering comes "
    "after this block:"
)
# The line that a fresh session gets after its rows.
HISTORY_HINT = (
    "This thread was reset. The handoff and the short rows above leave words out. "
    "For the exact words of an earlier message, use `search_history` or `read_history`."
)
# A row from normal chat is shown in the block up to this length.
CHAT_ROW_CHARS = 600


def workspace_id_of(channel_id: str) -> str | None:
    """The workspace id of a channel id ``workspace:<id>``, or None."""
    prefix = f"{WORKSPACE_CHANNEL_TYPE}:"
    return channel_id[len(prefix) :] if channel_id.startswith(prefix) else None


def signature(workspace: Workspace) -> tuple[str | None, tuple[str, ...], str]:
    """What a session is built from: the model, the servers, and the writing
    style. A change of any of them makes the manager rebuild the session on the
    next turn."""
    return (workspace.model, tuple(workspace.servers), workspace.style)


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


def _visible(row: dict[str, Any]) -> bool:
    """Whether a row of the thread is shown at all: a blank row, a silent
    reply, and a failed turn are not."""
    text = (row.get("content") or "").strip()
    return bool(text) and text != SILENT_REPLY and row.get("status") != FAILED_STATUS


def _row_name(row: dict[str, Any], default_name: str | None) -> str:
    """Who wrote a row: Joshua, normal chat, or the person of its
    ``meta.speaker``, because a shared thread holds the rows of several
    people. A row that names nobody takes ``default_name``."""
    meta = row.get("meta") or {}
    if meta.get("source") == FROM_CHAT_SOURCE:
        return "from normal chat"
    if row.get("direction") == "out":
        return "you"
    return meta.get("speaker") or default_name or "person"


def recent_lines(rows: list[dict[str, Any]], default_name: str | None) -> list[str]:
    """One short line for each visible row of the thread, oldest first."""
    lines = []
    for row in rows:
        if not _visible(row):
            continue
        from_chat = (row.get("meta") or {}).get("source") == FROM_CHAT_SOURCE
        person_row = row.get("direction") == "in" and not from_chat
        limit = PERSON_ROW_CHARS if person_row else RECENT_ROW_CHARS
        text = (row.get("content") or "").strip()
        lines.append(f"- {_row_name(row, default_name)}: {_one_line(text, limit)}")
    return lines


def split_turns(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The visible rows grouped into turns, oldest first.

    A turn is one message of the person and the reply to it. A new turn opens
    at a row that came in and follows a row that did not, so a reply, and an
    event that lands after it, stay with the message they answer.
    """
    turns: list[list[dict[str, Any]]] = []
    for row in rows:
        if not _visible(row):
            continue
        opens = row.get("direction") == "in" and (
            not turns or turns[-1][-1].get("direction") != "in"
        )
        if opens or not turns:
            turns.append([row])
        else:
            turns[-1].append(row)
    return turns


def carried_turns(
    rows: list[dict[str, Any]], count: int = CARRIED_TURNS
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """The last ``count`` turns of the thread, which a fresh session gets
    whole, and the visible rows before them, which it gets as short lines."""
    turns = split_turns(rows)
    carried = turns[-count:] if count > 0 else []
    taken = sum(len(turn) for turn in carried)
    visible = [row for row in rows if _visible(row)]
    return carried, visible[: len(visible) - taken]


def turn_lines(turns: list[list[dict[str, Any]]], default_name: str | None) -> list[str]:
    """The carried turns as text, oldest first, each message as it was
    written.

    Each message stands under the name of who wrote it, and a blank line
    separates two of them. A message past ``CARRIED_MESSAGE_CHARS`` is cut,
    and its text says so.
    """
    lines: list[str] = []
    for turn in turns:
        for row in turn:
            if lines:
                lines.append("")
            text = (row.get("content") or "").strip()
            if len(text) > CARRIED_MESSAGE_CHARS:
                text = text[:CARRIED_MESSAGE_CHARS] + CUT_NOTE
            lines.append(f"{_row_name(row, default_name)}: {text}")
    return lines


def _people_line(workspace: Workspace) -> str:
    """Who writes in a shared workspace: the owner, marked, then the members."""
    people = ", ".join(
        f"{person} (owner)" if person == workspace.owner else person for person in workspace.people
    )
    return f"People: {people}"


def _notify_line(workspace: Workspace, modes: dict[str, str]) -> str:
    """The notify mode of each person of a shared workspace: what
    ``notify_user`` may do for them."""
    people = ", ".join(
        f"{person} {modes.get(person) or default_mode(True)}" for person in workspace.people
    )
    return f"Notify each person: {people}"


def render_block(
    workspace: Workspace,
    items: list[WorkspaceItem],
    *,
    unseen: list[WorkspaceItem] | None = None,
    chat_rows: list[str] | None = None,
    recent: list[str] | None = None,
    turns: list[str] | None = None,
    extra: list[str] | None = None,
    handoff: str | None = None,
    notify_modes: dict[str, str] | None = None,
) -> str:
    """The workspace block as text.

    It holds the title, the goal, the people of a shared workspace, the notes,
    the items (``BLOCK_MAX_ITEMS``, pinned first, then newest), the items that
    came from normal chat and are
    not yet seen, and, for a fresh session, the handoff of the last session,
    the last turns of the thread whole, short lines for the rows before them,
    and the line that points at ``history``.
    ``chat_rows`` holds the text of the rows that normal chat sent to the
    thread and that no block has shown. ``extra`` holds the text of the
    sections that ``add_section`` registers. ``notify_modes`` is the notify
    mode of each person of a shared workspace, which only such a workspace
    has.
    """
    lines = [f"[Workspace: {workspace.title} ({workspace.slug})]"]
    if workspace.goal:
        lines.append(f"Goal: {workspace.goal}")
    if workspace.shared:
        lines.append(_people_line(workspace))
    lines.append(f"Notify: {workspace.notify}")
    if workspace.shared:
        lines.append(_notify_line(workspace, notify_modes or {}))
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
    if chat_rows:
        lines.append("From normal chat, since your last turn:")
        lines += [f"- {_one_line(text, CHAT_ROW_CHARS)}" for text in chat_rows]
    lines += extra or []
    lines += fresh_lines(handoff, recent, turns)
    return "\n".join(lines)


def fresh_lines(
    handoff: str | None, recent: list[str] | None, turns: list[str] | None = None
) -> list[str]:
    """What a fresh session gets: the handoff, the rows of the thread, the
    last turns whole, and the line that points at the ``history`` tools.
    Empty when there is neither a handoff nor a row."""
    lines: list[str] = []
    if handoff:
        lines += ["The handoff that the last session wrote before it ended:", handoff.strip()]
    if recent:
        head = (
            "Older rows of this thread, cut short:"
            if turns
            else "The last rows of this thread, because the session starts fresh:"
        )
        lines.append(head)
        lines += recent
    if turns:
        lines.append(RECENT_TURNS_HEAD)
        lines += turns
    if lines:
        lines.append(HISTORY_HINT)
    return lines


# A section of the block: it gets the workspace and returns text, or None for
# nothing to say. ``next_steps_section`` and ``jobs_section`` are two.
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
        turns: list[str] = []
        handoff: str | None = None
        if fresh:
            rows = await self._repo.transcript_tail(conversation_id, FRESH_TAIL_ROWS)
            carried, older = carried_turns(rows)
            turns = turn_lines(carried, workspace.owner)
            recent = recent_lines(older[-BLOCK_RECENT_ROWS:], workspace.owner)
            handoff = await self._repo.conversation_handoff(conversation_id)
        extra = []
        for section in self._sections:
            try:
                text = await section(workspace)
            except Exception as exc:  # noqa: BLE001 — a section never breaks a turn
                logger.warning({"message": "block section failed", "error": type(exc).__name__})
                continue
            if text:
                extra.append(text)
        modes = await self._notify_modes(workspace)
        chat_rows = await self._repo.unseen_chat_rows(conversation_id) if conversation_id else []
        if (unseen or chat_rows) and mark_seen is not None:
            mark_seen(unseen, [r["id"] for r in chat_rows])
        return render_block(
            workspace,
            items,
            unseen=unseen,
            chat_rows=[r["content"] for r in chat_rows],
            recent=recent,
            turns=turns,
            extra=extra,
            handoff=handoff,
            notify_modes=modes,
        )

    async def _notify_modes(self, workspace: Workspace) -> dict[str, str]:
        """The notify mode of each person of a shared workspace, the default
        of a person with no row of their own. A personal workspace has
        nobody else to notify, so it gets none."""
        if not workspace.shared:
            return {}
        rows = await self._repo.notify_prefs.list_for(workspace.id)
        return {row.person_id: row.mode for row in rows}

    async def __call__(self, ctx: Any) -> str | None:
        if ctx.channel.channel_type != WORKSPACE_CHANNEL_TYPE:
            return None
        workspace_id = workspace_id_of(ctx.channel.id)
        workspace = await self._repo.get_workspace(workspace_id) if workspace_id else None
        if workspace is None:
            return None

        def mark_seen(unseen: list[WorkspaceItem], row_ids: list[int]) -> None:
            ids = [i.id for i in unseen]

            async def done() -> None:
                await self._repo.mark_items_seen(ids)
                await self._repo.mark_chat_rows_seen(row_ids)

            ctx.after_turn.append(done)

        return await self.render(
            workspace, ctx.conversation.id, fresh=ctx.fresh_session, mark_seen=mark_seen
        )


# --- sections ----------------------------------------------------------------

# The next-steps call waits this long, then the section is skipped.
NEXT_STEPS_TIMEOUT_S = 2.0
NEXT_STEPS_LIMIT = 3
# The jobs section lists at most this many items.
JOBS_MAX = 10
JOB_KINDS = ("developer_task", "assignment")


def next_steps_section(
    repo: Repo, tasks_url: str | None, *, client_factory: Any = None
) -> BlockSection | None:
    """The "Next steps" section, or None when ``workspaces.tasks_url`` is not set.

    For each ``tasks_project`` item it calls ``GET <tasks_url>/api/next-steps?
    project=<ref>&limit=3`` with a 2 second timeout. Any error, a bad answer,
    or no steps skips that project, and so the section.
    """
    if not tasks_url:
        return None
    import httpx

    base = tasks_url.rstrip("/")

    async def section(workspace: Workspace) -> str | None:
        items = await repo.list_workspace_items(workspace.id)
        projects = [i for i in items if i.kind == "tasks_project"][:3]
        if not projects:
            return None
        lines: list[str] = []
        factory = client_factory or (lambda: httpx.AsyncClient(timeout=NEXT_STEPS_TIMEOUT_S))
        async with factory() as client:
            for project in projects:
                try:
                    resp = await client.get(
                        f"{base}/api/next-steps",
                        params={"project": project.ref, "limit": NEXT_STEPS_LIMIT},
                        timeout=NEXT_STEPS_TIMEOUT_S,
                    )
                    resp.raise_for_status()
                    steps = _steps_of(resp.json())
                except Exception as exc:  # noqa: BLE001 — a slow tasks addon never blocks a turn
                    logger.info({"message": "next steps skipped", "error": type(exc).__name__})
                    continue
                if steps:
                    lines.append(f"Next steps ({project.title or project.ref}):")
                    lines += [f"- {step}" for step in steps[:NEXT_STEPS_LIMIT]]
        return "\n".join(lines) or None

    return section


def _steps_of(data: Any) -> list[str]:
    """``#<number> <title>`` for each step in the answer of the tasks addon.

    The tasks addon answers ``[{<project summary>, "tasks": [<task>, ...]}]``:
    one entry per project, with its next open tasks. A plain list of tasks, or
    an object with ``steps`` or ``items``, is read too."""
    if isinstance(data, dict):
        data = data.get("steps") or data.get("items") or data.get("next_steps") or []
    if isinstance(data, list) and any(isinstance(e, dict) and "tasks" in e for e in data):
        data = [t for e in data if isinstance(e, dict) for t in (e.get("tasks") or [])]
    out = []
    for entry in data if isinstance(data, list) else []:
        if isinstance(entry, str):
            out.append(_one_line(entry, 160))
        elif isinstance(entry, dict) and entry.get("title"):
            number = entry.get("number") or entry.get("id")
            head = f"#{number} " if number not in (None, "") else ""
            out.append(_one_line(f"{head}{entry['title']}", 160))
    return out


def jobs_section(repo: Repo) -> BlockSection:
    """The jobs section: the ``developer_task`` and ``assignment`` items with the
    date they were added. The block makes no network call, and the thread does
    not tie a status line to an item, so it shows no job status. A job reports
    in the thread as an event, and the last rows show it."""

    async def section(workspace: Workspace) -> str | None:
        items = [i for i in await repo.list_workspace_items(workspace.id) if i.kind in JOB_KINDS]
        if not items:
            return None
        items.sort(key=lambda i: i.added_at.timestamp() if i.added_at else 0.0, reverse=True)
        lines = [
            "Jobs started from this workspace "
            "(status is not tracked here; a job reports in the thread):"
        ]
        for item in items[:JOBS_MAX]:
            added = f", added {item.added_at.date().isoformat()}" if item.added_at else ""
            lines.append(f"- {item.kind}: {item.title or item.ref} ({item.ref}{added})")
        return "\n".join(lines)

    return section


# --- RAG --------------------------------------------------------------------


def exclude_paths(workspace: Workspace, items: Iterable[WorkspaceItem]) -> set[str]:
    """The KB paths of the documents and wiki pages that are items of the
    workspace. The block holds them already, so RAG skips them.

    A document maps to ``documents/<owner>/<slug>.md`` and a wiki page to its
    path under ``wiki/``. The ref or the url may be the slug, the path, or an
    address that ends in either. The documents of a shared workspace stand
    under ``documents/shared/``, because they belong to everybody in it.
    """
    owner = SHARED_NAMESPACE if workspace.shared else workspace.owner
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
                    paths.add(f"documents/{owner}/{slug}.md")
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
    try:
        workspace = await repo.get_workspace(workspace_id) if workspace_id else None
        if workspace is None:
            return set()
        return exclude_paths(workspace, await repo.list_workspace_items(workspace.id))
    except Exception as exc:  # noqa: BLE001 — RAG falls back to no exclusions
        logger.warning({"message": "rag exclusions failed", "error": type(exc).__name__})
        return set()
