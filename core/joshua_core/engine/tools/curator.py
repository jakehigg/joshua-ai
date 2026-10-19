"""In-process ``curator`` tool server: read and act on the curator's findings.

``list_findings`` lists findings, ``apply_finding`` applies exactly the patch a
finding holds, and ``dismiss_finding`` closes one with a reason. Every tool
gates on the current speaker being a member: a guest, or a speaker core does
not know, gets a "not available" reply and nothing changes.

``apply_finding`` takes the lock that the jobs and the nightly reflection
share, applies the patch through ``jobs.curator.apply``, gives each changed
page that had current tracking keys new ones, commits the wiki with the
message ``curator: finding <id>``, and marks the finding ``applied`` with the
member's id. A patch that no longer fits the page changes nothing, and the
finding becomes ``stale``.

Core registers this server only when ``jobs.enabled`` and
``jobs.curator.enabled`` are both true.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from joshua_shared import layout, wikigit
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.tools import ToolDeps
from joshua_core.jobs.curator.apply import ApplyRefused, Workspace, apply_patch
from joshua_core.jobs.curator.findings import from_row
from joshua_core.jobs.curator.job import describe, local_day
from joshua_core.jobs.runner import Clock, utc_now
from joshua_core.store.repo import FINDING_STATUSES

logger = get_logger("tools.curator")

NOT_AVAILABLE = "That is not available to you. Only a member can use the curator findings."
BUSY = "A job or the nightly reflection is writing the wiki now. Try again in a few minutes."
MAX_LIST = 50


@dataclass(frozen=True)
class CuratorContext:
    """What the tools need beyond the conversation: the config, the data root,
    the wiki lock, and the clock."""

    settings: JoshuaConfig
    data_dir: Path
    lock: asyncio.Lock
    clock: Clock = utc_now


def _reply(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


async def _member_id(deps: ToolDeps) -> str | None:
    """The id of the current speaker if they are a member, else None."""
    pid = deps.person_id
    if not pid:
        return None
    person = await deps.repo.get_person(pid)
    if person is None or person.role != "member":
        return None
    return person.id


def _finding_id(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


async def do_list_findings(
    deps: ToolDeps, ctx: CuratorContext, *, status: str | None = "open", limit: int = 20
) -> dict[str, Any]:
    if await _member_id(deps) is None:
        return _reply(NOT_AVAILABLE)
    wanted = None if status in ("all", "") else (status or "open")
    if wanted is not None and wanted not in FINDING_STATUSES:
        return _reply(f"The status must be one of {', '.join(FINDING_STATUSES)}, or all.")
    count = max(1, min(int(limit or 20), MAX_LIST))
    rows = await deps.repo.list_findings(wanted, count)
    if not rows:
        return _reply(
            f"There are no {wanted} curator findings." if wanted else "There are no findings."
        )
    lines = [f"{describe(from_row(row))} [{row.get('status')}]" for row in rows]
    return _reply("Curator findings:\n" + "\n".join(lines))


async def do_apply_finding(
    deps: ToolDeps, ctx: CuratorContext, *, finding_id: Any
) -> dict[str, Any]:
    member = await _member_id(deps)
    if member is None:
        return _reply(NOT_AVAILABLE)
    fid = _finding_id(finding_id)
    row = await deps.repo.get_finding(fid) if fid is not None else None
    if row is None:
        return _reply(f"I found no curator finding {finding_id}.")
    if row.get("status") != "open":
        return _reply(f"Finding #{fid} is {row.get('status')}. I apply only an open finding.")
    patch = row.get("patch")
    if not patch:
        return _reply(
            f"Finding #{fid} has no patch to apply. Change the page by hand, "
            "then dismiss the finding."
        )
    if ctx.lock.locked():
        return _reply(BUSY)
    async with ctx.lock:
        now = ctx.clock()
        wiki = layout.wiki_root(ctx.data_dir)
        ws = Workspace(wiki, local_day(now, ctx.settings))
        try:
            apply_patch(ws, dict(patch))
        except ApplyRefused as exc:
            await deps.repo.set_finding_status(fid, "stale", at=now)
            return _reply(f"I did not apply finding #{fid}: {exc}. The finding is stale now.")
        ws.track_clean()
        if ws.written and wikigit.is_enabled(ctx.settings):
            wikigit.commit(wiki, ws.paths(), f"curator: finding {fid}")
        await deps.repo.set_finding_status(fid, "applied", at=now, resolved_by=member)
    logger.info({"message": "curator finding applied", "finding_id": fid, "files": len(ws.written)})
    changed = ", ".join(f"`{rel}`" for rel in ws.written) or "nothing"
    return _reply(f"Applied finding #{fid}. Changed: {changed}.")


async def do_dismiss_finding(
    deps: ToolDeps, ctx: CuratorContext, *, finding_id: Any, why: str
) -> dict[str, Any]:
    member = await _member_id(deps)
    if member is None:
        return _reply(NOT_AVAILABLE)
    reason = (why or "").strip()
    if not reason:
        return _reply("Tell me why, so a later run knows why the finding is closed.")
    fid = _finding_id(finding_id)
    row = await deps.repo.get_finding(fid) if fid is not None else None
    if row is None:
        return _reply(f"I found no curator finding {finding_id}.")
    if row.get("status") not in ("open", "stale"):
        return _reply(f"Finding #{fid} is {row.get('status')}. I dismiss only an open finding.")
    await deps.repo.set_finding_status(
        fid, "dismissed", at=ctx.clock(), resolved_by=member, note=reason[:1000]
    )
    return _reply(f"Dismissed finding #{fid}. A later run does not raise it again.")


# --- server ----------------------------------------------------------------

_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "description": "open (the default), applied, dismissed, stale, or all.",
        },
        "limit": {"type": "integer", "description": "How many findings, 1 to 50. Default 20."},
    },
}

_ID_SCHEMA = {
    "type": "object",
    "properties": {"id": {"type": "integer", "description": "The finding id."}},
    "required": ["id"],
}

_DISMISS_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer", "description": "The finding id."},
        "why": {"type": "string", "description": "Why the finding is not a problem."},
    },
    "required": ["id", "why"],
}


def build_curator_server(deps: ToolDeps, ctx: CuratorContext) -> Any:
    """Build the in-process ``curator`` MCP server bound to one conversation."""

    @tool(
        "list_findings",
        "List the curator's findings about the wiki: broken links, misplaced or "
        "duplicate pages, pages the journal has made old. Only a member may call this.",
        _LIST_SCHEMA,
    )
    async def list_findings(args: dict[str, Any]) -> dict[str, Any]:
        return await do_list_findings(
            deps, ctx, status=args.get("status") or "open", limit=args.get("limit") or 20
        )

    @tool(
        "apply_finding",
        "Apply exactly the patch of one open curator finding to the wiki. Call it "
        "only after the person agreed to that finding. Only a member may call this.",
        _ID_SCHEMA,
    )
    async def apply_finding(args: dict[str, Any]) -> dict[str, Any]:
        return await do_apply_finding(deps, ctx, finding_id=args.get("id"))

    @tool(
        "dismiss_finding",
        "Close one curator finding that is not a problem, with the reason. A later "
        "run does not raise it again. Only a member may call this.",
        _DISMISS_SCHEMA,
    )
    async def dismiss_finding(args: dict[str, Any]) -> dict[str, Any]:
        return await do_dismiss_finding(
            deps, ctx, finding_id=args.get("id"), why=args.get("why") or ""
        )

    return create_sdk_mcp_server(
        name="curator", version="1.0.0", tools=[list_findings, apply_finding, dismiss_finding]
    )


def register(
    manager: Any,
    *,
    settings: JoshuaConfig,
    data_dir: Path | str,
    lock: asyncio.Lock,
    clock: Clock | None = None,
) -> bool:
    """Wire the ``curator`` builtin into the conversation manager when
    ``jobs.enabled`` and ``jobs.curator.enabled`` are both true. Returns True
    when it did."""
    if not (settings.jobs.enabled and settings.jobs.curator.enabled):
        return False
    ctx = CuratorContext(
        settings=settings, data_dir=Path(data_dir), lock=lock, clock=clock or utc_now
    )
    manager.register_builtin("curator", lambda deps: build_curator_server(deps, ctx))
    return True
