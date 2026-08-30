"""Person and role attribution for gateway requests.

Core asserts the person, the conversation, and the role of every MCP request
with the ``X-Joshua-Person``, ``X-Joshua-Conversation``, and ``X-Joshua-Role``
headers. The gateway trusts those headers only from ``core``. From any other
caller they are ignored and the request has no person and no role. No setting
changes this, so nothing may widen it.

**The role is the access control; the person is not.** The corpus of Joshua is
shared, and the role alone decides whether a request writes. The person is kept
for the audit trail and for the provenance of a journal entry.

A trusted person must match the person id pattern and exist in ``people``, or
be the literal ``unknown``. A trusted role must be ``member`` or ``guest``.
Anything else is a 400.
"""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass

from joshua_shared.config import PERSON_ID_RE

_PERSON_RE = PERSON_ID_RE

# The literal person id for a request core could not attribute to a known person.
UNKNOWN = "unknown"

# The one identity trusted to say which person a request belongs to.
_TRUSTED = frozenset({"core"})

# The roles core may assert. A guest reads; a member also writes.
_ROLES = frozenset({"member", "guest"})


class PersonError(Exception):
    """A trusted person header is malformed or names no known person (400)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Attribution:
    """The person and conversation a request is attributed to. Both are None when
    the caller may not assert them."""

    person: str | None
    conversation: str | None
    role: str | None = None


def trusted_identities() -> frozenset[str]:
    """The fleet identities allowed to assert the person header."""
    return _TRUSTED


def resolve(
    identity: str,
    person_header: str | None,
    conversation_header: str | None,
    known_persons: Container[str],
    role_header: str | None = None,
) -> Attribution:
    """Attribute a request to a person, a conversation, and a role.

    ``identity`` is the authenticated fleet caller. Returns an empty attribution
    when the caller may not assert the headers. Raises ``PersonError`` for a
    trusted but invalid person or role header.
    """
    if identity not in trusted_identities():
        return Attribution(person=None, conversation=None, role=None)

    person = (person_header or "").strip() or None
    if person is not None and person != UNKNOWN:
        if not _PERSON_RE.match(person) or person not in known_persons:
            raise PersonError(f"unknown person: {person}")

    role = (role_header or "").strip() or None
    if role is not None and role not in _ROLES:
        raise PersonError(f"unknown role: {role}")

    conversation = (conversation_header or "").strip() or None
    return Attribution(person=person, conversation=conversation, role=role)
