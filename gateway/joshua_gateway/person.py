"""Person and role attribution for gateway requests.

Core asserts the person, the conversation, and the role of every MCP request
with the ``X-Joshua-Person``, ``X-Joshua-Conversation``, and ``X-Joshua-Role``
headers. A group chat also sends ``X-Joshua-Group``. The gateway trusts those
headers only from the fleet identities in ``gateway.person_header_callers``
(default ``core`` and ``assignments``). The assignments container acts for
the person who started an assignment, so it sends the same headers. From any
other caller the headers are ignored and the request has no person, no role,
and no group. The config refuses a list without ``core``, and the MCP routes
admit the identities of this list only.

**The role is the access control; the person is not.** The corpus of Joshua is
shared, and the role alone decides whether a request writes. The person is kept
for the audit trail and for the provenance of a journal entry.

A trusted person must match the person id pattern and exist in ``people``, or
be the literal ``unknown``. A trusted role must be ``member`` or ``guest``. A
trusted group must exist in ``groups``. A trusted workspace
(``X-Joshua-Workspace``) must be a UUID. Anything else is a 400.
"""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass
from uuid import UUID

from joshua_shared import config
from joshua_shared.config import PERSON_ID_RE, GatewayConfig

_PERSON_RE = PERSON_ID_RE

# The literal person id for a request core could not attribute to a known person.
UNKNOWN = "unknown"

# The identities trusted to say which person a request belongs to when no
# config is given: the default of ``gateway.person_header_callers``.
DEFAULT_TRUSTED = frozenset(GatewayConfig().person_header_callers)

# The roles core may assert. A guest reads; a member also writes.
_ROLES = frozenset({"member", "guest"})


class PersonError(Exception):
    """A trusted person header is malformed or names no known person (400)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Attribution:
    """The person, conversation, role, and group a request is attributed to.
    Each is None when the caller may not assert them. ``group`` is None for a
    direct message."""

    person: str | None
    conversation: str | None
    role: str | None = None
    group: str | None = None
    workspace: str | None = None


def trusted_identities() -> frozenset[str]:
    """The fleet identities allowed to assert the person header: the
    ``gateway.person_header_callers`` of the current config."""
    return frozenset(config.load().gateway.person_header_callers)


def resolve(
    identity: str,
    person_header: str | None,
    conversation_header: str | None,
    known_persons: Container[str],
    role_header: str | None = None,
    group_header: str | None = None,
    known_groups: Container[str] = (),
    trusted: Container[str] = DEFAULT_TRUSTED,
    workspace_header: str | None = None,
) -> Attribution:
    """Attribute a request to a person, a conversation, a role, and a group.

    ``identity`` is the authenticated fleet caller and ``trusted`` the
    identities that may assert the headers. Returns an empty attribution
    when the caller is not in ``trusted``. Raises ``PersonError`` for a
    trusted but invalid person, role, or group header.
    """
    if identity not in trusted:
        return Attribution(person=None, conversation=None, role=None)

    person = (person_header or "").strip() or None
    if person is not None and person != UNKNOWN:
        if not _PERSON_RE.match(person) or person not in known_persons:
            raise PersonError(f"unknown person: {person}")

    role = (role_header or "").strip() or None
    if role is not None and role not in _ROLES:
        raise PersonError(f"unknown role: {role}")

    group = (group_header or "").strip() or None
    if group is not None and group not in known_groups:
        raise PersonError(f"unknown group: {group}")

    workspace = (workspace_header or "").strip() or None
    if workspace is not None:
        try:
            workspace = str(UUID(workspace))
        except ValueError:
            raise PersonError("workspace must be a UUID") from None

    conversation = (conversation_header or "").strip() or None
    return Attribution(
        person=person, conversation=conversation, role=role, group=group, workspace=workspace
    )
