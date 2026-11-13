"""Normal chat knows the person's workspaces.

One context provider, for a direct, per-person chat of a member that is not a
workspace thread, and only when the person has an active workspace:

- the list: "Your workspaces: <title> - <goal>" for at most ``MAX_LISTED``
  active workspaces, and one line that says what to do (record it with
  ``send_to_workspace`` and say so in one line);
- the hint: when the message matches the ``workspaces`` row of one of them
  above ``workspaces.hint_sim``, one line, "May relate to workspace *X*".

A person with no active workspace gets nothing. A guest, a group chat, and a
workspace thread get nothing. The ``workspaces`` rows are never shown as text.
"""

from __future__ import annotations

import asyncio
from typing import Any

from joshua_shared.attachments import is_no_caption
from joshua_shared.config import JoshuaConfig, is_member
from joshua_shared.log import get_logger

from joshua_core.memory import embed as embed_module
from joshua_core.memory.search import search
from joshua_core.memory.sources.workspaces import workspace_id_of_uri
from joshua_core.memory.store import WORKSPACE_KIND, MemoryStore
from joshua_core.store.models import Workspace
from joshua_core.store.repo import WORKSPACE_CHANNEL_TYPE, Repo

logger = get_logger("engine.workspace_awareness")

MAX_LISTED = 10
TITLE_CHARS = 60
GOAL_CHARS = 90
# Below this length a message is a greeting, not something to match.
MIN_QUERY_CHARS = 8

INSTRUCTION = (
    "When something the person says belongs to one, record it with send_to_workspace "
    "and say so in one line."
)


def _cut(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def listing(workspaces: list[Workspace]) -> str:
    """The list note for the active ``workspaces`` (at most ``MAX_LISTED``)."""
    lines = ["Your workspaces:"]
    for w in workspaces[:MAX_LISTED]:
        goal = f" - {_cut(w.goal, GOAL_CHARS)}" if w.goal else ""
        lines.append(f"- {_cut(w.title, TITLE_CHARS)}{goal}")
    lines.append(INSTRUCTION)
    return "\n".join(lines)


def hint_line(workspace: Workspace) -> str:
    return f"May relate to workspace *{_cut(workspace.title, TITLE_CHARS)}*."


async def _hint(
    ctx: Any,
    active: list[Workspace],
    *,
    store: MemoryStore,
    settings: JoshuaConfig,
) -> str | None:
    text = (ctx.text or "").strip()
    if len(text) < MIN_QUERY_CHARS or is_no_caption(text):
        return None
    vec = getattr(ctx, "embedding", None)
    if vec is None:
        vec = await asyncio.to_thread(embed_module.embed, text, settings.memory.embed_model)
        if vec is None:
            return None
        try:
            ctx.embedding = vec
        except AttributeError:
            pass
    try:
        ranked = await search(
            store,
            ctx.conversation.person_id,
            vec,
            k=3,
            min_sim=settings.workspaces.hint_sim,
            kinds=(WORKSPACE_KIND,),
        )
    except Exception as exc:  # noqa: BLE001 — a hint never breaks a turn
        logger.warning({"message": "workspace hint skipped", "error": type(exc).__name__})
        return None
    by_id = {w.id: w for w in active}
    for chunk in ranked:  # best first
        found = by_id.get(workspace_id_of_uri(chunk.path) or "")
        if found is not None:
            return hint_line(found)
    return None


def build_provider(repo: Repo, store: MemoryStore | None, settings: JoshuaConfig) -> Any:
    async def provider(ctx: Any) -> str | None:
        if ctx.channel.channel_type == WORKSPACE_CHANNEL_TYPE:
            return None
        if ctx.channel.session_mode != "per_person" or not is_member(getattr(ctx, "role", "guest")):
            return None
        person = ctx.conversation.person_id
        if not person:
            return None
        active = await repo.list_workspaces(person, "active")
        if not active:
            return None
        notes = [listing(active)]
        if store is not None:
            hint = await _hint(ctx, active, store=store, settings=settings)
            if hint:
                notes.append(hint)
        return "\n".join(notes)

    return provider


def register(manager: Any, store: MemoryStore | None, settings: JoshuaConfig, repo: Repo) -> None:
    """Wire the provider. It runs after the KB injection, which shares its
    embedding of the message."""
    manager.add_context_provider(build_provider(repo, store, settings))
