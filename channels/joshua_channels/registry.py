"""The adapter registry: one plugin per messaging platform.

An adapter turns platform events into turns for core and turns core's
``/v1/deliver`` calls into platform sends. The registry maps a ``channel_type``
(``telegram`` | ``imessage`` | ``webhook``) to its adapter, the same per-type
dispatch the old ``core/app/delivery.py`` held. Concrete adapters register at
startup based on which ``channels.*`` sections are present in config.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from joshua_shared.log import get_logger

logger = get_logger("channels.registry")


@dataclass(frozen=True)
class AdapterHealth:
    """One adapter's readiness state for ``/readyz``.

    ``ok`` is True for a started adapter that has not raised. It is False for an
    adapter that raised on its last poll; ``reason`` then holds a short text with
    no credential. ``detail`` adds adapter-specific fields to the report.
    """

    ok: bool
    reason: str | None = None
    detail: Mapping[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        """Render the state for the ``/readyz`` body."""
        body: dict[str, object] = {"ok": self.ok}
        if self.reason is not None:
            body["reason"] = self.reason
        if self.detail:
            body.update(self.detail)
        return body


@runtime_checkable
class Adapter(Protocol):
    """One platform plugin.

    ``resolve_ref`` maps a logical ref (``group:<group_id>`` or ``dm:<person_id>``)
    to a platform chat id from config, or returns None when the ref is unknown.
    ``send`` does any platform splitting and formatting. ``health`` reports the
    adapter state for ``/readyz``.
    """

    channel_type: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, chat_id: str, text: str, attachments: list[Path]) -> None: ...

    def resolve_ref(self, ref: str) -> str | None: ...

    async def health(self) -> AdapterHealth: ...


class AdapterRegistry:
    """Hold the running adapters, keyed by ``channel_type``."""

    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}

    def register(self, adapter: Adapter) -> None:
        """Add an adapter. A second adapter for a type replaces the first."""
        self._adapters[adapter.channel_type] = adapter

    def get(self, channel_type: str) -> Adapter | None:
        """Return the adapter for ``channel_type``, or None."""
        return self._adapters.get(channel_type)

    def types(self) -> list[str]:
        """Return the registered channel types, sorted."""
        return sorted(self._adapters)

    def __contains__(self, channel_type: object) -> bool:
        return channel_type in self._adapters

    async def start_all(self) -> None:
        """Start every adapter."""
        for adapter in self._adapters.values():
            await adapter.start()

    async def stop_all(self) -> None:
        """Stop every adapter. One failure does not block the rest."""
        for channel_type, adapter in self._adapters.items():
            try:
                await adapter.stop()
            except Exception:  # noqa: BLE001 — one failed stop must not block the rest
                logger.exception("adapter stop failed", extra={"channel_type": channel_type})
