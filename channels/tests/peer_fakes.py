"""Fakes and builders for the peer tests: Telegram objects, a clock, and envelopes.

Every time comes from ``FakeClock``, so the tests pass on any date.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from joshua_shared import config as config_module
from joshua_shared.ids import new_ulid

NOW = datetime(2031, 3, 4, 12, 0, tzinfo=UTC)
PEER_BOT = 900
QUIET_BOT = 901

CONFIG = """
name: Test House
timezone: UTC
people:
  - id: alex
    name: Alex Diaz
    handles:
      telegram: "12345"
  - id: sam
    name: Sam Lee
    handles:
      telegram: "222"
  - id: gus
    name: Gus Green
    role: guest
    handles:
      telegram: "333"
channels:
  telegram:
    bot_token: t-token
    unknown_sender: reply
peers:
  - id: rivera
    name: Rivera's Joshua
    telegram_bot_id: "900"
    kinds:
      - recipe
      - note
    contacts:
      - name: Pat Rivera
  - id: quiet
    name: Quiet Joshua
    telegram_bot_id: "901"
    on_receive: silent
"""


def cfg(text: str = CONFIG):  # type: ignore[no-untyped-def]
    return config_module.parse(text, env={}, source="<test>")


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


def ulid(n: int) -> str:
    return new_ulid(int(NOW.timestamp() * 1000), bytes([n]) * 10)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def item(
    item_id: str,
    *,
    kind: str = "recipe",
    to: str = "Alex Diaz",
    title: str = "Short ribs",
    markdown: str = "Braise for **three** hours.",
    attachments: list[dict[str, Any]] | None = None,
    sent_at: datetime | None = None,
    expires_at: datetime | None = None,
    sender: str = "jake",
) -> dict[str, Any]:
    sent = sent_at or NOW - timedelta(minutes=1)
    return {
        "v": 1,
        "id": item_id,
        "sent_at": sent.isoformat(),
        "expires_at": (expires_at or NOW + timedelta(days=7)).isoformat(),
        "kind": kind,
        "sender_person": sender,
        "to_person": {"name": to, "aliases": []},
        "body": {"title": title, "markdown": markdown, "attachments": attachments or []},
    }


def ack(item_id: str, ref: str, status: str = "accepted") -> dict[str, Any]:
    return {
        "v": 1,
        "id": item_id,
        "sent_at": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at": (NOW + timedelta(days=1)).isoformat(),
        "kind": "ack",
        "body": {"ref": ref, "status": status},
    }


def raw(envelope: dict[str, Any]) -> bytes:
    return json.dumps(envelope).encode("utf-8")


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


class FakeTgFile:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def download_to_drive(self, path: Path) -> None:
        _write(Path(path), self._data)

    async def download_as_bytearray(self) -> bytearray:
        return bytearray(self._data)


class FakeBot:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.get_file_calls: list[str] = []
        self.documents: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.fail_send = False

    async def get_file(self, file_id: str) -> FakeTgFile:
        self.get_file_calls.append(file_id)
        return FakeTgFile(self.files[file_id])

    async def send_document(self, **kwargs: Any) -> None:
        if self.fail_send:
            raise RuntimeError("telegram down")
        self.documents.append(kwargs)

    async def send_message(self, **kwargs: Any) -> None:
        self.messages.append(kwargs)

    async def send_chat_action(self, **kwargs: Any) -> None:
        self.actions.append(kwargs)


@dataclass
class FakePeerCore:
    status: int = 202
    raise_exc: bool = False
    received: list[str] = field(default_factory=list)

    async def peer_received(self, item_id: str) -> int:
        if self.raise_exc:
            raise RuntimeError("core down")
        self.received.append(item_id)
        return self.status


def document(file_id: str, name: str, size: int | None) -> SimpleNamespace:
    return SimpleNamespace(file_id=file_id, file_unique_id=file_id, file_name=name, file_size=size)


def bot_update(
    doc: SimpleNamespace | None,
    *,
    user_id: int = PEER_BOT,
    chat_type: str = "private",
    text: str | None = None,
    is_bot: bool = True,
) -> SimpleNamespace:
    message = SimpleNamespace(
        text=text, caption=None, photo=None, document=doc, message_id=1, chat_id=user_id
    )
    user = SimpleNamespace(id=user_id, full_name="Peer Bot", is_bot=is_bot)
    chat = SimpleNamespace(id=user_id, type=chat_type, title=None)
    return SimpleNamespace(effective_message=message, effective_user=user, effective_chat=chat)
