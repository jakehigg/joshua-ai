"""Shared scaffolding for the iMessage adapter tests. Everything is OFFLINE.

The fakes stand in for BlueBubbles and core, so no test touches a network, a Mac,
or a real bot token.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from joshua_channels.adapters.imessage.adapter import IMessageAdapter
from joshua_channels.adapters.imessage.bb_client import BlueBubblesError, SendAttempt
from joshua_channels.core_client import TurnAck
from joshua_channels.guard import Guard
from joshua_shared import config as config_module
from joshua_shared.config import JoshuaConfig
from joshua_shared.contracts import Attachment, TurnEvent

FIXTURES = Path(__file__).parent / "imessage_fixtures"

SECRET = "s3cr3t-path"

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex Diaz
    handles:
      imessage: "+15551234567"
groups:
  - id: everyone
    channel: imessage
    chat_id: "iMessage;+;chat100000000000000001"
channels:
  imessage:
    bluebubbles_url: http://bb.local
    bluebubbles_password: pw
    webhook_path_secret: s3cr3t-path
    unknown_sender: drop
  limits:
    max_text_chars: 20
    per_handle_per_minute: 20
"""


def load_envelope(name: str) -> dict[str, Any]:
    """A full ``{"type": ..., "data": ...}`` webhook body."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


def load_data(name: str) -> dict[str, Any]:
    """Just the ``data`` object (what normalize_message consumes)."""
    return load_envelope(name)["data"]


def cfg(text: str = CONFIG) -> JoshuaConfig:
    return config_module.parse(text, env={}, source="<test>")


class FakeBB:
    """A stand-in BlueBubbles client that records sends and serves fixed blobs."""

    configured = True

    def __init__(
        self,
        *,
        blobs: dict[str, bytes] | None = None,
        reachable: bool = True,
        rows: list[dict[str, Any]] | None = None,
        fail_download: set[str] | None = None,
        fail_attachment: bool = False,
    ) -> None:
        self.blobs = blobs or {}
        self.reachable_value = reachable
        self.rows = rows or []
        self.fail_download = fail_download or set()
        self.fail_attachment = fail_attachment
        self.sent: list[tuple[str, list[str]]] = []
        self.attachments_sent: list[tuple[str, Path]] = []

    async def download_attachment(self, guid: str) -> bytes:
        if guid in self.fail_download:
            raise BlueBubblesError("download boom")
        return self.blobs.get(guid, b"blob-bytes")

    async def send_chunks(self, chat_guid: str, chunks: list[str]) -> list[SendAttempt]:
        self.sent.append((chat_guid, list(chunks)))
        return [SendAttempt(temp_guid="t", attempts=1) for _ in chunks]

    async def send_attachment(self, chat_guid: str, path: Path) -> SendAttempt:
        if self.fail_attachment:
            raise BlueBubblesError("attachment boom")
        self.attachments_sent.append((chat_guid, path))
        return SendAttempt(temp_guid="t", attempts=1)

    async def ping(self) -> bool:
        return self.reachable_value

    async def query_messages_since(
        self, after_ms: int, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        return self.rows


@dataclass
class FakeCore:
    ack: TurnAck = field(default_factory=lambda: TurnAck(accepted=True, status=202, turn_id="t1"))
    events: list[TurnEvent] = field(default_factory=list)
    raise_exc: bool = False

    async def submit_turn(self, event: TurnEvent) -> TurnAck:
        if self.raise_exc:
            raise RuntimeError("core down")
        self.events.append(event)
        return self.ack


@dataclass
class FakePipeline:
    result: list[Attachment]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def process(
        self, inbox_dir: Path, *, person_id: str | None, group_id: str | None
    ) -> list[Attachment]:
        self.calls.append({"dir": inbox_dir, "person_id": person_id, "group_id": group_id})
        return self.result


def make_adapter(
    tmp_path: Path,
    *,
    core: FakeCore | None = None,
    bb: FakeBB | None = None,
    pipeline: FakePipeline | None = None,
    cfg_text: str = CONFIG,
    coalesce_window_s: float = 0.0,
    stale_max_age_s: float = 0.0,
    reconcile_interval_s: int = 0,
) -> IMessageAdapter:
    settings = cfg(cfg_text)
    guard = Guard(settings, settings.channels.limits, config_provider=lambda: settings)
    return IMessageAdapter(
        bluebubbles_url="http://bb.local",
        bluebubbles_password="pw",
        webhook_path_secret=SECRET,
        guard=guard,
        core_client=core,  # type: ignore[arg-type]
        settings_provider=lambda: settings,
        pipeline=pipeline,
        data_dir=str(tmp_path),
        max_attachment_bytes=settings.channels.limits.max_attachment_bytes,
        coalesce_window_s=coalesce_window_s,
        stale_max_age_s=stale_max_age_s,
        reconcile_interval_s=reconcile_interval_s,
        bb=bb if bb is not None else FakeBB(),
    )
