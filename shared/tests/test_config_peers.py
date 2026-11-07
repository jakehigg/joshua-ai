"""The `peers:` section: the fields, and the start-up check for names that clash."""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_shared import config

BASE = """
name: Home
timezone: America/New_York
people:
  - id: alex
    name: Alex Smith
    handles:
      telegram: "998877"
  - id: mary-jane
    name: MJ
groups:
  - id: everyone
    channel: imessage
    chat_id: "iMessage;+;chat100000000000000001"
    members:
      - "+15551234567"
"""

RIVERA = """
  - id: rivera
    name: Rivera's Joshua
    telegram_bot_id: "1234567890"
    contacts:
      - name: Sam Rivera
        aliases:
          - sam r
"""


def parse(peers: str) -> config.JoshuaConfig:
    return config.parse(BASE + "peers:" + peers, {}, source="test.yaml")


def refuse(peers: str) -> str:
    with pytest.raises(config.ConfigError) as info:
        parse(peers)
    return str(info.value)


def contact(name: str, *aliases: str, peer: str = "rivera", bot: str = "1234567890") -> str:
    lines = [
        f"  - id: {peer}",
        f"    name: {peer} Joshua",
        f'    telegram_bot_id: "{bot}"',
        "    contacts:",
        f"      - name: {name}",
    ]
    if aliases:
        lines.append("        aliases:")
        lines.extend(f"          - {alias}" for alias in aliases)
    return "\n" + "\n".join(lines) + "\n"


def test_no_peers_section_means_no_peers() -> None:
    cfg = config.parse(BASE, {}, source="test.yaml")
    assert cfg.peers == []


def test_a_good_peer_loads_with_defaults() -> None:
    cfg = parse(RIVERA)
    peer = cfg.peers[0]
    assert peer.id == "rivera"
    assert peer.telegram_bot_id == "1234567890"
    assert peer.kinds == ["recipe", "note", "link"]
    assert peer.on_receive == "notify"
    assert peer.contacts[0].name == "Sam Rivera"
    assert peer.contacts[0].aliases == ["sam r"]


def test_kinds_and_on_receive_load() -> None:
    peers = RIVERA.replace(
        "    contacts:", "    kinds:\n      - note\n    on_receive: silent\n    contacts:"
    )
    peer = parse(peers).peers[0]
    assert peer.kinds == ["note"]
    assert peer.on_receive == "silent"


def test_a_bot_id_given_as_a_number_is_kept_as_text() -> None:
    peer = config.Peer(id="rivera", name="R", telegram_bot_id=1234567890)  # type: ignore[arg-type]
    assert peer.telegram_bot_id == "1234567890"


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("id: rivera", "id: Rivera", "peer id must match"),
        ("id: rivera", "id: ../x", "peer id must match"),
        ('"1234567890"', '"12ab"', "telegram_bot_id must be digits only"),
        ('"1234567890"', '"-100"', "telegram_bot_id must be digits only"),
        ("    contacts:", "    kinds:\n      - ack\n    contacts:", "kind 'ack' is not one of"),
        ("    contacts:", "    kinds:\n      - note\n      - note\n    contacts:", "repeat"),
        ("    contacts:", "    on_receive: loud\n    contacts:", "on_receive must be one of"),
        ("name: Rivera's Joshua", "name: ' '", "peer name must not be empty"),
        ("name: Sam Rivera", "name: '...'", "empty after normalization"),
    ],
)
def test_a_bad_field_is_refused(old: str, new: str, needle: str) -> None:
    assert needle in refuse(RIVERA.replace(old, new))


def test_an_empty_kinds_list_is_refused() -> None:
    with pytest.raises(ValueError, match="kinds must list"):
        config.Peer(id="rivera", name="R", telegram_bot_id="1", kinds=[])


def test_an_unknown_key_is_refused() -> None:
    assert "token" in refuse(RIVERA.replace("    contacts:", "    token: x\n    contacts:"))


def test_a_duplicate_peer_id_is_refused() -> None:
    peers = contact("Sam Rivera") + contact("Lee Park", bot="555")
    assert "duplicate peer id" in refuse(peers)


def test_a_duplicate_bot_id_is_refused() -> None:
    peers = contact("Sam Rivera") + contact("Lee Park", peer="park")
    assert "telegram_bot_id is already used" in refuse(peers)


def test_a_bot_id_that_is_a_person_handle_is_refused() -> None:
    assert "also a person's telegram handle" in refuse(contact("Sam Rivera", bot="998877"))


@pytest.mark.parametrize(
    ("name", "aliases", "local"),
    [
        ("Sam Rivera", ("alex",), "person id 'alex'"),
        ("Sam Rivera", ("ALEX",), "person id 'alex'"),
        ("Alex Smith", (), "person name 'Alex Smith'"),
        ("Sam Rivera", ("alex  smith",), "person name 'Alex Smith'"),
        ("Sam Rivera", ("Mary Jane",), "person id 'mary-jane'"),
        ("Sam Rivera", ("mj!",), "person name 'MJ'"),
        ("Sam Rivera", ("m'j",), "person name 'MJ'"),
        ("Everyone", (), "group id 'everyone'"),
    ],
)
def test_a_contact_that_clashes_with_a_local_name_is_refused(
    name: str, aliases: tuple[str, ...], local: str
) -> None:
    message = refuse(contact(name, *aliases))
    assert local in message
    assert "contact" in message and f"'{name}'" in message
    assert "peer 'rivera'" in message
    assert "full name" in message and "first-name alias" in message


def test_a_contact_name_on_two_peers_is_refused() -> None:
    peers = contact("Sam Rivera") + contact("sam rivera", peer="park", bot="555")
    message = refuse(peers)
    assert "unique across all peers" in message
    assert "peer 'park'" in message and "peer 'rivera'" in message


def test_an_alias_that_is_another_contacts_name_is_refused() -> None:
    peers = contact("Sam Rivera") + contact("Lee Park", "Sam-Rivera", peer="park", bot="555")
    assert "unique across all peers" in refuse(peers)


def test_two_contacts_on_one_peer_must_differ() -> None:
    peers = RIVERA + "      - name: Lee Park\n        aliases:\n          - sam r\n"
    assert "unique across all peers" in refuse(peers)


def test_a_contact_may_repeat_its_own_name_as_an_alias() -> None:
    cfg = parse(contact("Sam Rivera", "sam rivera"))
    assert cfg.peers[0].contacts[0].aliases == ["sam rivera"]


def test_the_example_peers_block_validates() -> None:
    """The commented block in joshua.example.yaml must load once uncommented."""
    text = (Path(__file__).resolve().parents[2] / "joshua.example.yaml").read_text()
    lines = text.splitlines()
    start = lines.index(next(line for line in lines if line.startswith("# peers:")))
    block = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        block.append(line[2:] if line.startswith("# ") else line[1:])
    yaml = BASE + "\n".join(block) + "\n"
    cfg = config.parse(yaml, {}, source="example.yaml")
    assert [p.id for p in cfg.peers] == ["rivera"]
