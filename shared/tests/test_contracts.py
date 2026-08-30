"""Unit tests for the channels → core wire models."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from joshua_shared.contracts import (
    Attachment,
    Chat,
    DeliverRequest,
    Handle,
    ResolveResponse,
    TurnAck,
    TurnEvent,
)
from pydantic import BaseModel, ValidationError

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"

# The wire models both packages share. Their JSON schema is frozen under
# ``snapshots/`` so any field rename or type change is a visible diff.
CONTRACT_MODELS: dict[str, type[BaseModel]] = {
    "turn_event": TurnEvent,
    "turn_ack": TurnAck,
    "deliver_request": DeliverRequest,
    "resolve_response": ResolveResponse,
}


def test_minimal_message_event_defaults() -> None:
    event = TurnEvent(
        channel="telegram:12345",
        chat=Chat(kind="dm", id="12345"),
        handle=Handle(type="telegram", id="998877"),
    )
    assert event.kind == "message"
    assert event.text == ""
    assert event.attachments == []
    assert event.reply_to is None
    assert event.framing is None


def test_full_event_round_trips() -> None:
    payload = {
        "channel": "telegram:12345",
        "chat": {"kind": "group", "id": "-100", "title": "Everyone"},
        "handle": {"type": "telegram", "id": "998877"},
        "text": "hi",
        "attachments": [{"path": "attachments/2026/08/a.jpg", "mime": "image/jpeg"}],
        "message_id": "m-1",
    }
    event = TurnEvent.model_validate(payload)
    assert event.chat.title == "Everyone"
    assert event.attachments[0] == Attachment(path="attachments/2026/08/a.jpg", mime="image/jpeg")
    assert event.attachments[0].original_name is None
    assert event.message_id == "m-1"


def test_attachment_carries_original_name() -> None:
    att = Attachment(
        path="attachments/2026/08/2026-08-27-140509-photo.jpg",
        mime="image/jpeg",
        name="2026-08-27-140509-photo.jpg",
        original_name="IMG_4471.jpg",
    )
    assert att.original_name == "IMG_4471.jpg"


def test_event_kind_allows_no_handle() -> None:
    event = TurnEvent(
        channel="webhook:ha",
        chat=Chat(kind="dm", id="ha"),
        kind="event",
        event_type="doorbell",
        payload={"who": "front"},
    )
    assert event.handle is None
    assert event.event_type == "doorbell"


def test_unknown_handle_type_rejected() -> None:
    with pytest.raises(ValidationError):
        Handle(type="signal", id="1")


def test_extra_keys_forbidden() -> None:
    with pytest.raises(ValidationError):
        TurnEvent(
            channel="telegram:1",
            chat=Chat(kind="dm", id="1"),
            handle=Handle(type="telegram", id="9"),
            surprise="nope",
        )


# --- schema snapshots ------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CONTRACT_MODELS))
def test_json_schema_matches_snapshot(name: str) -> None:
    """Freeze the JSON schema of each wire model.

    Set ``UPDATE_CONTRACT_SNAPSHOTS=1`` to rewrite the snapshot after a reviewed
    contract change.
    """
    schema = CONTRACT_MODELS[name].model_json_schema()
    path = SNAPSHOT_DIR / f"{name}.json"
    if os.environ.get("UPDATE_CONTRACT_SNAPSHOTS") == "1":
        SNAPSHOT_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert schema == expected, (
        f"{name} schema drifted; rerun with UPDATE_CONTRACT_SNAPSHOTS=1 if intended"
    )


def test_field_rename_breaks_snapshot() -> None:
    """A renamed field must not match the committed snapshot."""

    class RenamedTurnAck(BaseModel):
        accepted: bool
        http_status: int  # was ``status``
        turn_id: str | None = None
        reason: str | None = None

    expected = json.loads((SNAPSHOT_DIR / "turn_ack.json").read_text(encoding="utf-8"))
    assert RenamedTurnAck.model_json_schema() != expected
