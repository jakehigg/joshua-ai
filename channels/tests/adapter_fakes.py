"""A fake adapter for the channels tests.

It records ``send`` calls and resolves refs from a fixed map, so the registry,
resolver, and deliver routes run with no real platform.
"""

from __future__ import annotations

from pathlib import Path

from joshua_channels.registry import AdapterHealth


class FakeAdapter:
    def __init__(
        self,
        channel_type: str,
        refs: dict[str, str] | None = None,
        *,
        fail: bool = False,
        health: AdapterHealth | None = None,
    ) -> None:
        self.channel_type = channel_type
        self._refs = refs or {}
        self._fail = fail
        self._health = health or AdapterHealth(ok=True)
        self.sent: list[tuple[str, str, list[Path]]] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, chat_id: str, text: str, attachments: list[Path]) -> None:
        if self._fail:
            raise RuntimeError("send boom")
        self.sent.append((chat_id, text, list(attachments)))

    def resolve_ref(self, ref: str) -> str | None:
        return self._refs.get(ref)

    async def health(self) -> AdapterHealth:
        return self._health
