"""Peers: the names, the name rules, and the envelope two Joshua instances exchange.

A peer is another Joshua instance that the owner pairs by hand in the
``peers:`` section of ``joshua.yaml``. This module holds the parts that more
than one container needs:

- ``normalize_name``: the one rule that makes two names equal.
- ``resolve_contact``: the sending side. It finds the one saved contact that a
  person named, or refuses and lists the candidates.
- ``resolve_recipient``: the receiving side. It finds the one local person an
  envelope names, or returns unmatched. It never guesses.
- The envelope v1 models and their size limits.

This module imports nothing from ``config``, so ``config`` can import the name
rule and the kinds from here. Resolution reads only the ``id``, ``name``,
``contacts``, and ``aliases`` attributes of what it gets.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from joshua_shared.ids import PERSON_ID_PATTERN, PERSON_ID_RE, ULID_PATTERN, ULID_RE

# --- kinds and limits ------------------------------------------------------

# The kinds of item a peer can send. `ack` is not an item: it answers one.
ITEM_KINDS = ("recipe", "note", "link")
ENVELOPE_KINDS = (*ITEM_KINDS, "ack")
ACK_STATUSES = ("accepted", "rejected", "unmatched")
ON_RECEIVE = ("notify", "silent")

ENVELOPE_VERSION = 1
MAX_ENVELOPE_BYTES = 128 * 1024
MAX_TITLE_CHARS = 200
MAX_MARKDOWN_BYTES = 64 * 1024
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_NAME_CHARS = 255
MAX_NAME_CHARS = 200
MAX_ALIASES = 10

_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}\Z")
_APOSTROPHES = "'‘’ʼ`"

# --- names -----------------------------------------------------------------


def normalize_name(value: str) -> str:
    """Return the form of a name that two names are compared in.

    Lower case. Apostrophes are removed, so "Rivera's" is "riveras". Other
    punctuation becomes a space, so "mary-jane" and "Mary Jane" are equal.
    Spaces collapse to one, with none at the ends.
    """
    text = unicodedata.normalize("NFKC", value).casefold()
    out: list[str] = []
    for char in text:
        if char in _APOSTROPHES:
            continue
        if unicodedata.category(char).startswith(("P", "S")) or char.isspace():
            out.append(" ")
        else:
            out.append(char)
    return " ".join("".join(out).split())


class _Named(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...


class _Contact(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def aliases(self) -> list[str]: ...


class _Peer(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def contacts(self) -> Sequence[_Contact]: ...


def contact_keys(contact: _Contact) -> set[str]:
    """The normalized name and aliases of one contact. Empty forms are left out."""
    keys = {normalize_name(contact.name), *(normalize_name(a) for a in contact.aliases)}
    keys.discard("")
    return keys


def person_keys(person: _Named) -> set[str]:
    """The normalized id and name of one local person. Empty forms are left out."""
    keys = {normalize_name(person.id), normalize_name(person.name)}
    keys.discard("")
    return keys


# --- the sending side ------------------------------------------------------


@dataclass(frozen=True)
class ContactMatch:
    """The one target of a send.

    ``contact`` is None for an ad-hoc target: a name that is not a saved
    contact, sent to the peer the person named. ``name`` and ``aliases`` are
    what goes in the envelope's ``to_person``.
    """

    peer: _Peer
    contact: _Contact | None
    name: str
    aliases: tuple[str, ...] = ()

    def to_person(self) -> ToPerson:
        return ToPerson(name=self.name, aliases=list(self.aliases))


@dataclass(frozen=True)
class ContactRefusal:
    """No send. ``candidates`` holds ``(peer id, contact name)`` pairs to show the person.

    ``reason`` is ``no_match``, ``ambiguous``, or ``unknown_peer``. For
    ``unknown_peer`` each candidate is ``(peer id, peer name)``.
    """

    reason: Literal["no_match", "ambiguous", "unknown_peer"]
    candidates: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def _find_peer(peers: Sequence[_Peer], ref: str) -> _Peer | None:
    """The peer whose id is ``ref``, or whose name normalizes to the same form."""
    for peer in peers:
        if peer.id == ref:
            return peer
    key = normalize_name(ref)
    named = [peer for peer in peers if key and normalize_name(peer.name) == key]
    return named[0] if len(named) == 1 else None


def resolve_contact(
    peers: Sequence[_Peer], to: str, peer: str | None = None
) -> ContactMatch | ContactRefusal:
    """Find the one saved contact that ``to`` names.

    The match is an exact match of the normalized ``to`` against the
    normalized name or an alias of each contact. One match is the target. No
    match or more than one is a refusal that lists the candidates, and the
    caller sends nothing.

    With ``peer`` (a peer id or a peer name), only that peer's contacts count.
    When none of them matches, the target is ad hoc: ``to`` as given, for that
    peer.
    """
    key = normalize_name(to)
    pool = list(peers)
    chosen: _Peer | None = None
    if peer is not None:
        chosen = _find_peer(peers, peer)
        if chosen is None:
            return ContactRefusal("unknown_peer", tuple((p.id, p.name) for p in peers))
        pool = [chosen]

    matches = [
        (p, contact) for p in pool for contact in p.contacts if key and key in contact_keys(contact)
    ]
    if len(matches) == 1:
        target_peer, contact = matches[0]
        return ContactMatch(target_peer, contact, contact.name, tuple(contact.aliases))
    if len(matches) > 1:
        return ContactRefusal("ambiguous", tuple((p.id, c.name) for p, c in matches))
    if chosen is not None and to.strip():
        return ContactMatch(chosen, None, " ".join(to.split()))
    return ContactRefusal("no_match", tuple((p.id, c.name) for p in pool for c in p.contacts))


# --- the receiving side ----------------------------------------------------


@dataclass(frozen=True)
class Recipient:
    """The local person an envelope goes to, or why it is unmatched.

    ``reason`` is ``exact`` or ``first_word`` with a ``person_id``, and
    ``no_match`` or ``ambiguous`` with none. An unmatched item goes to the
    owners' inbox.
    """

    person_id: str | None
    reason: Literal["exact", "first_word", "no_match", "ambiguous"]
    candidates: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return self.person_id is not None


def resolve_recipient(people: Sequence[_Named], to_person: ToPerson) -> Recipient:
    """Find the one local person that ``to_person`` names. Never guess.

    1. The normalized ``name`` or any alias equals the normalized id or name
       of exactly one person: that person.
    2. Else, when no person matched in step 1, the first word of the
       normalized ``name`` equals the normalized id or name of exactly one
       person: that person.
    3. Else unmatched, with the reason.
    """
    keys = {normalize_name(to_person.name), *(normalize_name(a) for a in to_person.aliases)}
    keys.discard("")
    exact = [p.id for p in people if person_keys(p) & keys]
    if len(exact) == 1:
        return Recipient(exact[0], "exact")
    if len(exact) > 1:
        return Recipient(None, "ambiguous", tuple(exact))

    words = normalize_name(to_person.name).split()
    if not words:
        return Recipient(None, "no_match")
    first = [p.id for p in people if words[0] in person_keys(p)]
    if len(first) == 1:
        return Recipient(first[0], "first_word")
    if len(first) > 1:
        return Recipient(None, "ambiguous", tuple(first))
    return Recipient(None, "no_match")


# --- envelope v1 -----------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_ulid(value: str) -> str:
    if not ULID_RE.match(value):
        raise ValueError(f"must be a ULID matching {ULID_PATTERN}")
    return value


def _check_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("must carry a time zone")
    return value


class Attachment(_Model):
    """One attachment, sent as its own document and named here by its hash."""

    name: str = Field(min_length=1, max_length=MAX_ATTACHMENT_NAME_CHARS)
    mime: str
    sha256: str
    size: int = Field(ge=0, le=MAX_ATTACHMENT_BYTES)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if "/" in value or "\\" in value or "\x00" in value or value.startswith("."):
            raise ValueError("name must be a plain file name")
        return value

    @field_validator("mime")
    @classmethod
    def _check_mime(cls, value: str) -> str:
        if not _MIME_RE.match(value):
            raise ValueError("mime must be a lower-case type/subtype")
        return value

    @field_validator("sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        if not _SHA256_RE.match(value):
            raise ValueError("sha256 must be 64 lower-case hex characters")
        return value


class ItemBody(_Model):
    """The body of a ``recipe``, a ``note``, or a ``link``."""

    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    markdown: str
    attachments: list[Attachment] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)

    @field_validator("markdown")
    @classmethod
    def _check_markdown(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_MARKDOWN_BYTES:
            raise ValueError(f"markdown must be at most {MAX_MARKDOWN_BYTES} bytes")
        return value

    @field_validator("attachments")
    @classmethod
    def _check_unique(cls, value: list[Attachment]) -> list[Attachment]:
        hashes = [a.sha256 for a in value]
        if len(hashes) != len(set(hashes)):
            raise ValueError("each attachment must have a different sha256")
        return value


class AckBody(_Model):
    """The body of an ``ack``: which envelope it answers, and what happened to it."""

    ref: str
    status: Literal["accepted", "rejected", "unmatched"]

    @field_validator("ref")
    @classmethod
    def _check_ref(cls, value: str) -> str:
        return _check_ulid(value)


class ToPerson(_Model):
    """The person an item is for, as the sending owner names them."""

    name: str = Field(min_length=1, max_length=MAX_NAME_CHARS)
    aliases: list[Annotated[str, Field(min_length=1, max_length=MAX_NAME_CHARS)]] = Field(
        default_factory=list, max_length=MAX_ALIASES
    )


class _EnvelopeBase(_Model):
    v: Literal[1]
    id: str
    sent_at: datetime
    expires_at: datetime

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _check_ulid(value)

    @field_validator("sent_at", "expires_at")
    @classmethod
    def _check_time(cls, value: datetime) -> datetime:
        return _check_aware(value)

    @model_validator(mode="after")
    def _check_order(self) -> _EnvelopeBase:
        if self.expires_at <= self.sent_at:
            raise ValueError("expires_at must be after sent_at")
        return self

    def expired(self, now: datetime) -> bool:
        """True when ``now`` is at or past ``expires_at``. The caller gives the clock."""
        return now >= self.expires_at


class ItemEnvelope(_EnvelopeBase):
    """An envelope that carries one item to one person."""

    kind: Literal["recipe", "note", "link"]
    sender_person: str
    to_person: ToPerson
    body: ItemBody

    @field_validator("sender_person")
    @classmethod
    def _check_sender(cls, value: str) -> str:
        if not PERSON_ID_RE.match(value):
            raise ValueError(f"sender_person must match {PERSON_ID_PATTERN}")
        return value


class AckEnvelope(_EnvelopeBase):
    """An envelope that answers an item. It never reaches a model."""

    kind: Literal["ack"]
    body: AckBody


Envelope = Annotated[ItemEnvelope | AckEnvelope, Field(discriminator="kind")]
_ENVELOPE_ADAPTER: TypeAdapter[ItemEnvelope | AckEnvelope] = TypeAdapter(Envelope)


class EnvelopeError(ValueError):
    """An envelope is too large or does not match the v1 schema."""


def parse_envelope(raw: bytes) -> ItemEnvelope | AckEnvelope:
    """Parse one envelope document. Raises ``EnvelopeError`` on any refusal.

    The size check comes first, so an oversize document is never parsed.
    """
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise EnvelopeError(f"envelope is larger than {MAX_ENVELOPE_BYTES} bytes")
    try:
        return _ENVELOPE_ADAPTER.validate_json(raw)
    except ValueError as exc:
        raise EnvelopeError(str(exc)) from exc


def dump_envelope(envelope: ItemEnvelope | AckEnvelope) -> bytes:
    """Serialize an envelope to its JSON document. Raises ``EnvelopeError`` when too large."""
    raw = envelope.model_dump_json(exclude_none=True).encode("utf-8")
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise EnvelopeError(f"envelope is larger than {MAX_ENVELOPE_BYTES} bytes")
    return raw
