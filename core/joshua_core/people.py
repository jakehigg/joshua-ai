"""People operations shared by the registration tool, the CLI, and the admin API.

The pure helpers classify a handle and derive an id. ``add_person`` and
``remove_person`` are the operator path (the CLI and ``/admin/people``): they take
an explicit id and do not gate on a member speaker. The agent's ``registration``
tool derives the id from the name and gates on the speaker; it reuses the helpers
here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from joshua_shared import layout
from joshua_shared.config import PERSON_ID_PATTERN, PERSON_ID_RE

from joshua_core.store import people_file
from joshua_core.store.repo import Repo

_PERSON_ID_RE = PERSON_ID_RE
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")
_DIGITS_RE = re.compile(r"^\d{3,}$")
_NON_ID_RE = re.compile(r"[^a-z0-9]+")

VALID_ROLES = ("member", "guest")


class PeopleError(Exception):
    """A people operation failed a precondition (bad input or a handle conflict)."""


# --- handle shape ----------------------------------------------------------


def looks_like_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value.strip()))


def looks_like_phone(value: str) -> bool:
    return bool(_E164_RE.match(value.strip()))


def infer_channel_type(handle: str, current: str | None) -> str:
    """Guess the channel type from the handle shape, else the current channel.

    An email or an E.164 number is iMessage. A ``@name`` or a bare numeric id is
    Telegram. Anything else falls back to the channel the request arrived on.
    """
    value = handle.strip()
    if looks_like_email(value) or looks_like_phone(value):
        return "imessage"
    if value.startswith("@") or _DIGITS_RE.match(value):
        return "telegram"
    return current or "telegram"


def normalize_handle(channel_type: str, handle: str) -> str:
    """Canonical handle form: strip a Telegram ``@`` and lowercase an iMessage email."""
    value = handle.strip()
    if channel_type == "telegram":
        return value[1:] if value.startswith("@") else value
    if channel_type == "imessage" and looks_like_email(value):
        return value.lower()
    return value


def split_handle(handle: str) -> tuple[str, str]:
    """Split a ``<type>:<value>`` handle argument. Raises ``PeopleError`` on a bad shape."""
    ctype, _, value = handle.partition(":")
    ctype = ctype.strip().lower()
    value = value.strip()
    if not ctype or not value:
        raise PeopleError("handle must be '<type>:<value>', e.g. telegram:998877")
    return ctype, value


def display_from_name(name: str) -> str:
    """Collapse whitespace in a raw display name."""
    return " ".join(name.split())


def slugify_id(name: str) -> str:
    """Derive a person id slug from a name. Returns "" when nothing usable remains."""
    slug = _NON_ID_RE.sub("-", name.strip().lower()).strip("-")
    return slug[:32].rstrip("-")


def sidecar_entry(person_id: str, name: str, role: str, channel_type: str, handle: str) -> dict:
    return {"id": person_id, "name": name, "role": role, "handles": {channel_type: handle}}


# --- operator path ---------------------------------------------------------


async def add_person(
    repo: Repo,
    data_dir: Path | str,
    *,
    person_id: str,
    name: str,
    handle: str,
    role: str = "member",
) -> dict[str, Any]:
    """Add or update a person from an operator request (CLI or admin API).

    ``handle`` is ``<type>:<value>``. Raises ``PeopleError`` on a bad id, a bad
    role, or a handle that already belongs to a different person.
    """
    person_id = person_id.strip()
    if not _PERSON_ID_RE.match(person_id):
        raise PeopleError(f"id must match {PERSON_ID_PATTERN}")
    name = display_from_name(name or "")
    if not name:
        raise PeopleError("name is required")
    role = (role or "member").strip().lower()
    if role not in VALID_ROLES:
        raise PeopleError("role must be 'member' or 'guest'")

    ctype, raw = split_handle(handle)
    norm = normalize_handle(ctype, raw)
    owner = await repo.get_person_by_handle(ctype, norm)
    if owner is not None and owner.id != person_id:
        raise PeopleError(
            f"handle {ctype}:{norm} already belongs to {owner.display_name} (id {owner.id})"
        )

    await repo.upsert_person(person_id, name, role)
    await repo.upsert_person_handle(ctype, norm, person_id)
    people_file.upsert_person(
        Path(data_dir) / "people.yaml", sidecar_entry(person_id, name, role, ctype, norm)
    )
    layout.bootstrap_person(person_id, name, root=data_dir)
    return {"id": person_id, "name": name, "role": role, "handles": {ctype: norm}}


async def remove_person(repo: Repo, data_dir: Path | str, *, person_id: str) -> bool:
    """Mark a person removed and drop their handles. Data dirs and transcripts stay.

    Returns False when no such person exists.
    """
    person_id = person_id.strip()
    ok = await repo.remove_person(person_id)
    if not ok:
        return False
    people_file.mark_removed(Path(data_dir) / "people.yaml", person_id)
    return True


async def roster(repo: Repo) -> list[dict[str, Any]]:
    """The live roster with handles, newest schema for the CLI and admin API."""
    out: list[dict[str, Any]] = []
    for person in await repo.list_people():
        if person.role == "removed":
            continue
        handles = await repo.handles_for_person(person.id)
        out.append(
            {
                "id": person.id,
                "name": person.display_name,
                "role": person.role,
                "handles": {t: h for t, h in handles},
            }
        )
    return out
