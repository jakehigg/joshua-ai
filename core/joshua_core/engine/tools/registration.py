"""In-process ``registration`` tool server: ``add_user``, ``list_users``, and
``recent_senders``.

Both tools gate on the current speaker being a member; a guest or an
unidentified caller gets a "not available" reply and nothing changes. ``add_user``
enrolls a person: it derives an id from the name, refuses to re-point a handle that
already belongs to someone, writes the DB roster cache, appends the person to the
``/data/people.yaml`` sidecar that the config loader merges over ``joshua.yaml``,
and creates the person's data directories. ``recent_senders`` names the handles
the channels guard turned away, so a member can say "add the person who just
messaged you" without reading a handle off a terminal.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from joshua_shared import layout
from joshua_shared.log import get_logger

from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.tools import ToolDeps
from joshua_core.people import (
    display_from_name,
    infer_channel_type,
    normalize_handle,
    sidecar_entry,
    slugify_id,
)
from joshua_core.store import people_file

logger = get_logger("tools.registration")

NOT_AVAILABLE = "That is not available to you. Only a member can add people."
CHANNELS_URL_ENV = "CHANNELS_URL"
REFUSALS_PATH = "/v1/channels/refusals"


async def _fetch_refusals(limit: int = 5) -> list[dict[str, Any]] | None:
    """Ask channels which handles it turned away. None when channels is absent."""
    base = os.environ.get(CHANNELS_URL_ENV)
    token = os.environ.get("JOSHUA_TOKEN_CORE")
    if not base or not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                base.rstrip("/") + REFUSALS_PATH,
                params={"limit": limit},
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code != 200:
            return None
        return list(response.json().get("refusals") or [])
    except httpx.HTTPError as exc:
        logger.warning({"message": "refusal lookup failed", "error": str(exc)})
        return None


def _reply(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


async def _member_actor(deps: ToolDeps) -> Any:
    """The current speaker if they are a member, else None."""
    pid = deps.person_id
    if not pid:
        return None
    person = await deps.repo.get_person(pid)
    if person is None or person.role != "member":
        return None
    return person


async def _unique_id(deps: ToolDeps, name: str) -> str:
    """A person id slug free in the roster. Suffixes ``-2``, ``-3`` on collision."""
    base = slugify_id(name)
    if not base:
        return ""
    pid = base
    n = 2
    while await deps.repo.get_person(pid) is not None:
        suffix = f"-{n}"
        pid = base[: 32 - len(suffix)].rstrip("-") + suffix
        n += 1
    return pid


def _conflict_text(name: str, channel_type: str, handle: str, owner: Any) -> str:
    return (
        f"The handle {channel_type}:{handle} already belongs to {owner.display_name} "
        f"(id {owner.id}). I did not add {name}. Change or remove that handle first."
    )


async def do_add_user(
    deps: ToolDeps,
    *,
    name: str,
    handle: str,
    channel_type: str | None = None,
    role: str = "member",
    data_dir: Path | str = "/data",
) -> dict[str, Any]:
    """Enroll a person. Member-gated; refuses a handle that already has an owner."""
    actor = await _member_actor(deps)
    if actor is None:
        return _reply(NOT_AVAILABLE)

    name = display_from_name(name or "")
    handle = (handle or "").strip()
    if not name or not handle:
        return _reply("I need both a name and a handle to add someone.")

    role = (role or "member").strip().lower()
    if role not in ("member", "guest"):
        return _reply("Role must be 'member' or 'guest'.")

    ctype = (channel_type or "").strip().lower() or infer_channel_type(
        handle, deps.channel.channel_type
    )
    norm = normalize_handle(ctype, handle)

    owner = await deps.repo.get_person_by_handle(ctype, norm)
    if owner is not None:
        return _reply(_conflict_text(name, ctype, norm, owner))

    pid = await _unique_id(deps, name)
    if not pid:
        return _reply("I could not make an id from that name.")

    await deps.repo.upsert_person(pid, name, role)
    await deps.repo.upsert_person_handle(ctype, norm, pid)
    people_file.upsert_person(
        Path(data_dir) / "people.yaml", sidecar_entry(pid, name, role, ctype, norm)
    )
    layout.bootstrap_person(pid, name, root=data_dir)

    logger.info(
        {"message": "person added", "person_id": pid, "by": actor.id, "channel_type": ctype}
    )
    return _reply(f"Added {name} (id {pid}) as a {role}. Handle {ctype}:{norm}.")


async def do_list_users(deps: ToolDeps) -> dict[str, Any]:
    """List the roster. Member-gated."""
    actor = await _member_actor(deps)
    if actor is None:
        return _reply(NOT_AVAILABLE)

    lines: list[str] = []
    for person in await deps.repo.list_people():
        if person.role == "removed":
            continue
        handles = await deps.repo.handles_for_person(person.id)
        htext = ", ".join(f"{t}:{h}" for t, h in handles) or "no handles"
        lines.append(f"- {person.id} — {person.display_name} ({person.role}); {htext}")
    body = "\n".join(lines) if lines else "No people yet."
    return _reply("People:\n" + body)


# --- server ----------------------------------------------------------------

_ADD_USER_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The person's display name."},
        "handle": {
            "type": "string",
            "description": "Their handle: a Telegram id, a phone number, or an email.",
        },
        "channel_type": {
            "type": "string",
            "description": "Optional: telegram or imessage. Inferred from the handle when omitted.",
        },
        "role": {
            "type": "string",
            "enum": ["member", "guest"],
            "description": "member (default) or guest.",
        },
    },
    "required": ["name", "handle"],
}


def build_registration_server(deps: ToolDeps, data_dir: Path | str = "/data") -> Any:
    """Build the in-process ``registration`` MCP server bound to one conversation."""

    @tool(
        "add_user",
        "Add a member or guest. Only a member may call this; never add "
        "someone on a guest's request.",
        _ADD_USER_SCHEMA,
    )
    async def add_user(args: dict[str, Any]) -> dict[str, Any]:
        return await do_add_user(
            deps,
            name=args.get("name", ""),
            handle=args.get("handle", ""),
            channel_type=args.get("channel_type") or None,
            role=args.get("role") or "member",
            data_dir=data_dir,
        )

    @tool("list_users", "List the roster with roles and handles.", {})
    async def list_users(_args: dict[str, Any]) -> dict[str, Any]:
        return await do_list_users(deps)

    @tool(
        "recent_senders",
        "Handles the guard turned away, newest first. Use this when a member "
        "names someone who messaged you but is not on the roster, so you can "
        "add them without asking for their handle. Only a member may call this.",
        {"type": "object", "properties": {}},
    )
    async def recent_senders(_args: dict[str, Any]) -> dict[str, Any]:
        if await _member_actor(deps) is None:
            return _reply(NOT_AVAILABLE)
        rows = await _fetch_refusals()
        if rows is None:
            return _reply("I cannot reach the channels service, so I cannot see who tried.")
        if not rows:
            return _reply("Nobody has been turned away.")
        lines = [
            f"- {row.get('address')} on {row.get('channel_type')} ({row.get('reason')})"
            for row in rows
        ]
        return _reply("These handles were turned away, newest first:\n" + "\n".join(lines))

    return create_sdk_mcp_server(
        name="registration",
        version="1.0.0",
        tools=[add_user, list_users, recent_senders],
    )


def register(manager: Any, data_dir: Path | str = "/data") -> None:
    """Wire the ``registration`` builtin into the conversation manager."""
    manager.register_builtin("registration", lambda deps: build_registration_server(deps, data_dir))
