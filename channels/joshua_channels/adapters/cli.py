"""The terminal channel.

A person talks to Joshua from a shell. The turn arrives on the channels HTTP
route, passes the guard like any other channel, and goes to core. This adapter
is the outbound half: it takes what core sends to a ``cli:<person>`` channel and
appends it to that person's outbox, so a reminder or an event reply waits for
them the next time they open a session.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from joshua_shared.log import get_logger

from joshua_channels.registry import AdapterHealth

logger = get_logger("channels.cli")

CHANNEL_TYPE = "cli"
OUTBOX_NAME = "outbox.jsonl"


def outbox_path(data_dir: Path | str, person_id: str) -> Path:
    """`<data>/people/<person>/cli/outbox.jsonl`."""
    return Path(data_dir) / "people" / person_id / "cli" / OUTBOX_NAME


class CliAdapter:
    """Deliver to a person's terminal outbox."""

    channel_type = CHANNEL_TYPE

    def __init__(self, *, data_dir: Path | str = "/data") -> None:
        self._data_dir = Path(data_dir)

    async def start(self) -> None:
        """Nothing to start. The route accepts a turn whenever one arrives."""

    async def stop(self) -> None:
        """Nothing to stop."""

    async def health(self) -> AdapterHealth:
        """Always ready. The channel holds no connection and no credential."""
        return AdapterHealth(ok=True)

    async def send(self, chat_id: str, text: str, attachments: list[Path]) -> None:
        """Append one line to the person's outbox."""
        path = outbox_path(self._data_dir, chat_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now(UTC).isoformat(),
            "text": text,
            "attachments": [str(a) for a in attachments],
        }
        with path.open("a") as handle:
            handle.write(json.dumps(line) + "\n")
        logger.info({"message": "cli outbox append", "person": chat_id})

    def resolve_ref(self, ref: str) -> str | None:
        """`dm:<person>` names that person. No other ref belongs to this channel."""
        kind, _, value = ref.partition(":")
        return value if kind == "dm" and value else None
