"""Peers: the name rule, both kinds of name resolution, and the envelope v1 schema."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from joshua_shared import peers
from joshua_shared.config import Peer, PeerContact, Person
from joshua_shared.ids import new_ulid
from joshua_shared.peers import (
    AckEnvelope,
    EnvelopeError,
    ItemEnvelope,
    ToPerson,
    normalize_name,
    parse_envelope,
    resolve_contact,
    resolve_recipient,
)

# Fixed values: no test reads the clock.
ULID = new_ulid(1_700_000_000_000, bytes(range(10)))
OTHER_ULID = new_ulid(1_700_000_000_001, bytes(10))
SHA = "a" * 64


# --- normalize_name ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Alex", "alex"),
        ("  Alex   Rivera ", "alex rivera"),
        ("Rivera's Joshua", "riveras joshua"),
        ("Rivera’s Joshua", "riveras joshua"),
        ("O'Brien", "obrien"),
        ("mary-jane", "mary jane"),
        ("Mary.Jane!", "mary jane"),
        ("ALEX\tRIVERA", "alex rivera"),
        ("...", ""),
        ("", ""),
        ("Zoë", "zoë"),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


# --- resolve_contact (the sending side) -------------------------------------


def _peers() -> list[Peer]:
    return [
        Peer(
            id="rivera",
            name="Rivera's Joshua",
            telegram_bot_id="1",
            contacts=[
                PeerContact(name="Sam Rivera", aliases=["sam r"]),
                PeerContact(name="Lee Rivera"),
            ],
        ),
        Peer(
            id="park",
            name="Park Joshua",
            telegram_bot_id="2",
            contacts=[PeerContact(name="Jo Park")],
        ),
    ]


@pytest.mark.parametrize("to", ["Sam Rivera", "sam  rivera", "Sam R", "sam-r"])
def test_one_contact_matches(to: str) -> None:
    result = resolve_contact(_peers(), to)
    assert isinstance(result, peers.ContactMatch)
    assert result.peer.id == "rivera"
    assert result.contact is not None and result.contact.name == "Sam Rivera"
    assert result.to_person() == ToPerson(name="Sam Rivera", aliases=["sam r"])


def test_no_contact_matches_and_every_contact_is_a_candidate() -> None:
    result = resolve_contact(_peers(), "Sam")
    assert isinstance(result, peers.ContactRefusal)
    assert result.reason == "no_match"
    assert result.candidates == (
        ("rivera", "Sam Rivera"),
        ("rivera", "Lee Rivera"),
        ("park", "Jo Park"),
    )


def test_an_empty_name_matches_nothing() -> None:
    result = resolve_contact(_peers(), "  ")
    assert isinstance(result, peers.ContactRefusal)
    assert result.reason == "no_match"


def test_two_matches_refuse_and_list_both() -> None:
    """Config validation stops this; the resolver still refuses when it gets it."""
    both = _peers()
    both[1].contacts.append(PeerContact(name="Sam Rivera"))
    result = resolve_contact(both, "sam rivera")
    assert isinstance(result, peers.ContactRefusal)
    assert result.reason == "ambiguous"
    assert result.candidates == (("rivera", "Sam Rivera"), ("park", "Sam Rivera"))


def test_a_named_peer_with_an_unknown_name_gives_an_ad_hoc_target() -> None:
    result = resolve_contact(_peers(), "  Pat   Rivera ", peer="rivera")
    assert isinstance(result, peers.ContactMatch)
    assert result.peer.id == "rivera"
    assert result.contact is None
    assert result.to_person() == ToPerson(name="Pat Rivera", aliases=[])


def test_a_named_peer_still_prefers_a_saved_contact() -> None:
    result = resolve_contact(_peers(), "jo park", peer="Park Joshua")
    assert isinstance(result, peers.ContactMatch)
    assert result.contact is not None and result.contact.name == "Jo Park"


def test_a_named_peer_limits_the_search_to_its_contacts() -> None:
    result = resolve_contact(_peers(), "Sam Rivera", peer="park")
    assert isinstance(result, peers.ContactMatch)
    assert result.peer.id == "park"
    assert result.contact is None


def test_an_unknown_peer_refuses_and_lists_the_peers() -> None:
    result = resolve_contact(_peers(), "Sam Rivera", peer="nobody")
    assert isinstance(result, peers.ContactRefusal)
    assert result.reason == "unknown_peer"
    assert result.candidates == (("rivera", "Rivera's Joshua"), ("park", "Park Joshua"))


def test_a_named_peer_with_an_empty_name_refuses() -> None:
    result = resolve_contact(_peers(), "", peer="rivera")
    assert isinstance(result, peers.ContactRefusal)
    assert result.reason == "no_match"


# --- resolve_recipient (the receiving side) ---------------------------------


def _people() -> list[Person]:
    return [
        Person(id="alex", name="Alex Smith"),
        Person(id="jo", name="Jo Park"),
        Person(id="sam-a", name="Sam"),
        Person(id="sam-b", name="Sam B"),
        Person(id="mary-jane", name="MJ"),
    ]


@pytest.mark.parametrize(
    ("name", "aliases", "person"),
    [
        ("Alex Smith", [], "alex"),
        ("alex", [], "alex"),
        ("Jo  Park", [], "jo"),
        ("Mary Jane", [], "mary-jane"),
        ("Somebody", ["MJ"], "mary-jane"),
        ("Sam B.", [], "sam-b"),
    ],
)
def test_an_exact_match_names_one_person(name: str, aliases: list[str], person: str) -> None:
    result = resolve_recipient(_people(), ToPerson(name=name, aliases=aliases))
    assert result.matched
    assert (result.person_id, result.reason) == (person, "exact")


def test_the_first_word_matches_when_it_names_one_person() -> None:
    result = resolve_recipient(_people(), ToPerson(name="Alex Rivera"))
    assert (result.person_id, result.reason) == ("alex", "first_word")


def test_the_first_word_that_names_two_people_is_unmatched() -> None:
    people = [*_people(), Person(id="alex2", name="Alex")]
    result = resolve_recipient(people, ToPerson(name="Alex Rivera"))
    assert not result.matched
    assert result.reason == "ambiguous"
    assert result.candidates == ("alex", "alex2")


def test_an_exact_match_on_two_people_is_unmatched() -> None:
    result = resolve_recipient(_people(), ToPerson(name="alex", aliases=["jo"]))
    assert not result.matched
    assert result.reason == "ambiguous"
    assert set(result.candidates) == {"alex", "jo"}


def test_no_match_is_unmatched() -> None:
    result = resolve_recipient(_people(), ToPerson(name="Pat Rivera", aliases=["pat"]))
    assert (result.person_id, result.reason) == (None, "no_match")


def test_a_name_of_only_punctuation_is_unmatched() -> None:
    result = resolve_recipient(_people(), ToPerson(name="..."))
    assert (result.person_id, result.reason) == (None, "no_match")


def test_the_first_word_does_not_match_a_part_of_a_name() -> None:
    """`Sam` is a whole name only for `sam-a`; `Sam B` does not count for `sam`."""
    result = resolve_recipient(_people(), ToPerson(name="Sam Q"))
    assert (result.person_id, result.reason) == ("sam-a", "first_word")


# --- the envelope -----------------------------------------------------------


def _item(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "v": 1,
        "id": ULID,
        "sent_at": "2026-01-02T03:04:05Z",
        "expires_at": "2026-01-09T03:04:05Z",
        "kind": "recipe",
        "sender_person": "alex",
        "to_person": {"name": "Sam Rivera", "aliases": ["sam r"]},
        "body": {
            "title": "Short ribs",
            "markdown": "Braise them.",
            "attachments": [{"name": "ribs.jpg", "mime": "image/jpeg", "sha256": SHA, "size": 10}],
        },
    }
    data.update(changes)
    return data


def _ack(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "v": 1,
        "id": OTHER_ULID,
        "sent_at": "2026-01-02T03:04:05Z",
        "expires_at": "2026-01-09T03:04:05Z",
        "kind": "ack",
        "body": {"ref": ULID, "status": "accepted"},
    }
    data.update(changes)
    return data


def _raw(data: dict[str, Any]) -> bytes:
    return json.dumps(data).encode()


def _body(**changes: Any) -> dict[str, Any]:
    body = dict(_item()["body"])
    body.update(changes)
    return body


def test_a_good_item_parses_and_round_trips() -> None:
    envelope = parse_envelope(_raw(_item()))
    assert isinstance(envelope, ItemEnvelope)
    assert envelope.kind == "recipe"
    assert envelope.body.attachments[0].sha256 == SHA
    assert parse_envelope(peers.dump_envelope(envelope)) == envelope


@pytest.mark.parametrize("kind", ["note", "link"])
def test_each_item_kind_parses(kind: str) -> None:
    assert parse_envelope(_raw(_item(kind=kind))).kind == kind


def test_a_good_ack_parses() -> None:
    envelope = parse_envelope(_raw(_ack()))
    assert isinstance(envelope, AckEnvelope)
    assert envelope.body.ref == ULID
    assert envelope.body.status == "accepted"


def test_expired_uses_the_given_clock() -> None:
    envelope = parse_envelope(_raw(_item()))
    assert not envelope.expired(datetime(2026, 1, 9, 3, 4, 4, tzinfo=UTC))
    assert envelope.expired(datetime(2026, 1, 9, 3, 4, 5, tzinfo=UTC))


@pytest.mark.parametrize(
    "data",
    [
        _item(v=2),
        _item(v="1"),
        _item(kind="poem"),
        _item(id="not-a-ulid"),
        _item(id=ULID.lower()),
        _item(id="8" + ULID[1:]),
        _item(id=ULID[:-1] + "U"),
        _item(sender_person="../alex"),
        _item(sent_at="2026-01-02T03:04:05"),
        _item(expires_at="2026-01-02T03:04:05Z"),
        _item(extra="x"),
        _item(to_person={"name": ""}),
        _item(to_person={"name": "x", "aliases": ["a"] * (peers.MAX_ALIASES + 1)}),
        _item(body=_body(title="")),
        _item(body=_body(title="t" * (peers.MAX_TITLE_CHARS + 1))),
        _item(body=_body(markdown="m" * (peers.MAX_MARKDOWN_BYTES + 1))),
        _item(body=_body(markdown="é" * (peers.MAX_MARKDOWN_BYTES // 2 + 1))),
        _item(body={"ref": ULID, "status": "accepted"}),
        _ack(body={"title": "t", "markdown": "m"}),
        _ack(body={"ref": ULID, "status": "maybe"}),
        _ack(body={"ref": "bad", "status": "accepted"}),
        _ack(sender_person="alex"),
    ],
)
def test_a_bad_envelope_is_refused(data: dict[str, Any]) -> None:
    with pytest.raises(EnvelopeError):
        parse_envelope(_raw(data))


def _attachment(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"name": "a.pdf", "mime": "application/pdf", "sha256": SHA, "size": 1}
    data.update(changes)
    return data


@pytest.mark.parametrize(
    "attachments",
    [
        [_attachment(sha256=f"{i:064x}") for i in range(peers.MAX_ATTACHMENTS + 1)],
        [_attachment(), _attachment(name="b.pdf")],
        [_attachment(size=peers.MAX_ATTACHMENT_BYTES + 1)],
        [_attachment(size=-1)],
        [_attachment(name="../etc/passwd")],
        [_attachment(name=".hidden")],
        [_attachment(name="a\\b")],
        [_attachment(sha256="A" * 64)],
        [_attachment(sha256="a" * 63)],
        [_attachment(mime="Image/JPEG")],
        [_attachment(mime="nonsense")],
    ],
)
def test_bad_attachments_are_refused(attachments: list[dict[str, Any]]) -> None:
    with pytest.raises(EnvelopeError):
        parse_envelope(_raw(_item(body=_body(attachments=attachments))))


def test_the_most_attachments_allowed_parse() -> None:
    attachments = [_attachment(sha256=f"{i:064x}") for i in range(peers.MAX_ATTACHMENTS)]
    envelope = parse_envelope(_raw(_item(body=_body(attachments=attachments))))
    assert isinstance(envelope, ItemEnvelope)
    assert len(envelope.body.attachments) == peers.MAX_ATTACHMENTS


def test_an_oversize_document_is_refused_before_it_is_parsed() -> None:
    raw = _raw(_item()) + b" " * peers.MAX_ENVELOPE_BYTES
    with pytest.raises(EnvelopeError, match="larger than"):
        parse_envelope(raw)


def test_a_document_that_is_not_json_is_refused() -> None:
    with pytest.raises(EnvelopeError):
        parse_envelope(b"\xff not json")


def test_dump_refuses_an_envelope_over_the_limit() -> None:
    envelope = parse_envelope(_raw(_item()))
    assert isinstance(envelope, ItemEnvelope)
    big = envelope.model_copy(
        update={"body": envelope.body.model_copy(update={"markdown": "m" * 200_000})}
    )
    with pytest.raises(EnvelopeError, match="larger than"):
        peers.dump_envelope(big)
