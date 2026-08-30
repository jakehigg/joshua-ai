"""Person attribution for gateway requests.

Core asserts the person and conversation of every MCP request with the
``X-Joshua-Person`` and ``X-Joshua-Conversation`` headers. The gateway trusts
those headers only from ``core``. From any other caller the headers are ignored
and the request has no person. No setting changes this: the header decides who
reads and writes a person's files, so nothing may widen it.

A trusted person must match the person id pattern and exist in
``people``, or be the literal ``unknown``. Anything else is a 400.
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


def trusted_identities() -> frozenset[str]:
    """The fleet identities allowed to assert the person header."""
    return _TRUSTED


def resolve(
    identity: str,
    person_header: str | None,
    conversation_header: str | None,
    known_persons: Container[str],
) -> Attribution:
    """Attribute a request to a person and conversation.

    ``identity`` is the authenticated fleet caller. Returns an empty attribution
    when the caller may not assert the headers. Raises ``PersonError`` for a
    trusted but invalid person header.
    """
    if identity not in trusted_identities():
        return Attribution(person=None, conversation=None)

    person = (person_header or "").strip() or None
    if person is not None and person != UNKNOWN:
        if not _PERSON_RE.match(person) or person not in known_persons:
            raise PersonError(f"unknown person: {person}")

    conversation = (conversation_header or "").strip() or None
    return Attribution(person=person, conversation=conversation)
