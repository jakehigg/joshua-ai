"""The peers service: the outbox, the receiver, and the background task.

``build_peers_service`` returns None when ``peers`` is empty. Then the peer
routes answer 404 and no background task runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_channels.core_client import CoreClient
from joshua_channels.peers.outbox import Outbox
from joshua_channels.peers.protocol import Clock, SendDocument, utc_now
from joshua_channels.peers.receive import PeerReceiver

logger = get_logger("channels.peers")


class PeersService:
    """What the peer routes and the Telegram adapter share."""

    def __init__(
        self,
        *,
        data_dir: Path | str,
        settings_provider: Callable[[], JoshuaConfig],
        core_client: CoreClient | None,
        clock: Clock = utc_now,
    ) -> None:
        self.clock = clock
        self.transport: SendDocument | None = None
        self._settings = settings_provider
        self.outbox = Outbox(data_dir, sender=lambda: self.transport, clock=clock)
        self.receiver = PeerReceiver(
            data_dir=data_dir,
            settings_provider=settings_provider,
            outbox=self.outbox,
            core_client=core_client,
            clock=clock,
        )
        self._tasks: set[asyncio.Task[bool]] = set()

    def send_soon(self, item_id: str) -> None:
        """Start the first attempt for an item, so the route need not wait for Telegram."""
        task = asyncio.create_task(self.outbox.attempt(item_id), name=f"peer-send-{item_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """Wait for every first attempt that ``send_soon`` started."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def tick(self) -> None:
        """One pass of the background task: retry, expire, and prune."""
        await self.outbox.retry_due()
        self.outbox.prune()
        now = self.clock()
        self.receiver.seen.prune(now)
        for peer in self._settings().peers:
            self.receiver.staging.prune(peer.id, now)


def build_peers_service(
    settings: JoshuaConfig,
    *,
    data_dir: Path | str,
    settings_provider: Callable[[], JoshuaConfig],
    core_client: CoreClient | None,
    clock: Clock = utc_now,
) -> PeersService | None:
    """A ``PeersService``, or None when no peer is configured."""
    if not settings.peers:
        return None
    logger.info({"message": "peers on", "peers": len(settings.peers)})
    return PeersService(
        data_dir=data_dir,
        settings_provider=settings_provider,
        core_client=core_client,
        clock=clock,
    )
