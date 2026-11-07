"""The files of the peer path: the replay table, the staging area, and the inbox.

Every path comes from ``joshua_shared.layout``. Every write is atomic: the
bytes go to a temporary file in the same folder, then ``os.replace`` moves it
into place. An item folder is built under a temporary name and renamed into
the inbox in one step, so a reader never sees half an item.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from joshua_shared import layout
from joshua_shared.log import get_logger
from joshua_shared.peers import ItemEnvelope

logger = get_logger("channels.peers.store")

# How long a staged attachment waits for its envelope.
STAGING_TTL_S = 3600

# The most files one peer may hold in staging. The oldest goes first.
STAGING_MAX_FILES = 40

ENVELOPE_NAME = "envelope.json"
BODY_NAME = "body.md"
META_NAME = "meta.json"
ATTACHMENTS_NAME = "attachments"


def write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temporary file and a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_json(path: Path, value: dict[str, Any]) -> None:
    write_atomic(path, json.dumps(value, sort_keys=True).encode("utf-8"))


def read_json(path: Path) -> dict[str, Any] | None:
    """The JSON object in ``path``, or None when it is missing or not an object."""
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso(value: datetime) -> str:
    return value.isoformat()


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


# --- the replay table -------------------------------------------------------


@dataclass(frozen=True)
class SeenRecord:
    """One envelope id this instance received, and the ack it sent back."""

    status: str
    expires_at: datetime
    ack: bytes


class SeenTable:
    """The replay table: ``peers/seen/<id>.json``. It survives a restart."""

    def __init__(self, data_dir: Path | str) -> None:
        self._root = Path(data_dir)

    def get(self, item_id: str) -> SeenRecord | None:
        data = read_json(layout.peer_seen_path(item_id, self._root))
        if data is None:
            return None
        expires_at = parse_time(data.get("expires_at"))
        ack = data.get("ack")
        if expires_at is None or not isinstance(ack, str):
            return None
        return SeenRecord(str(data.get("status", "")), expires_at, ack.encode("utf-8"))

    def put(self, item_id: str, status: str, expires_at: datetime, ack: bytes) -> None:
        write_json(
            layout.peer_seen_path(item_id, self._root),
            {"status": status, "expires_at": _iso(expires_at), "ack": ack.decode("utf-8")},
        )

    def prune(self, now: datetime) -> int:
        """Remove each record whose ``expires_at`` is past. Returns the count.

        An envelope past its ``expires_at`` is refused as expired, so its
        record is not needed to stop a replay.
        """
        root = layout.peers_seen_root(self._root)
        if not root.is_dir():
            return 0
        removed = 0
        for path in root.glob("*.json"):
            data = read_json(path)
            expires_at = parse_time(data.get("expires_at")) if data else None
            if expires_at is None or now >= expires_at:
                with contextlib.suppress(OSError):
                    path.unlink()
                    removed += 1
        return removed


# --- the staging area -------------------------------------------------------


class Staging:
    """The attachments a peer sent before its envelope, keyed by sha256.

    The folder is ``inbox/peers/<peer_id>/``. A file is named by its hash, and
    its modification time comes from the injected clock, so the TTL is the
    same on every date.
    """

    def __init__(self, data_dir: Path | str) -> None:
        self._root = Path(data_dir)

    def dir(self, peer_id: str) -> Path:
        return layout.peer_staging_dir(peer_id, self._root)

    def new_temp(self, peer_id: str) -> Path:
        """A fresh temporary path in the peer's staging folder."""
        folder = self.dir(peer_id)
        folder.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(prefix=".dl-", dir=folder)
        os.close(handle)
        return Path(tmp)

    def commit(self, peer_id: str, tmp: Path, now: datetime) -> str:
        """Move a downloaded file to its hash name. Returns the sha256."""
        digest = sha256_file(tmp)
        target = self.dir(peer_id) / digest
        os.replace(tmp, target)
        stamp = now.timestamp()
        os.utime(target, (stamp, stamp))
        self._cap(peer_id)
        return digest

    def path(self, peer_id: str, sha256: str) -> Path | None:
        target = self.dir(peer_id) / sha256
        return target if target.is_file() else None

    def prune(self, peer_id: str, now: datetime) -> int:
        """Remove staged files older than ``STAGING_TTL_S``. Returns the count."""
        folder = self.dir(peer_id)
        if not folder.is_dir():
            return 0
        cutoff = now.timestamp() - STAGING_TTL_S
        removed = 0
        for path in folder.iterdir():
            try:
                old = path.stat().st_mtime < cutoff
            except OSError:
                continue
            if old:
                with contextlib.suppress(OSError):
                    path.unlink()
                    removed += 1
        return removed

    def _cap(self, peer_id: str) -> None:
        files = [p for p in self.dir(peer_id).iterdir() if not p.name.startswith(".")]
        if len(files) <= STAGING_MAX_FILES:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for path in files[: len(files) - STAGING_MAX_FILES]:
            with contextlib.suppress(OSError):
                path.unlink()


# --- the inbox ---------------------------------------------------------------


def item_exists(data_dir: Path | str, item_id: str) -> bool:
    return layout.peer_item_dir(item_id, data_dir).exists()


def store_item(
    data_dir: Path | str,
    envelope: ItemEnvelope,
    raw: bytes,
    attachments: dict[str, Path],
    meta: dict[str, Any],
) -> Path:
    """Write one item to ``peers/inbox/<id>/`` in one rename. Returns the folder.

    ``raw`` is the envelope document exactly as received. ``attachments`` maps
    each attachment name to the staged file, which moves into the item.
    """
    final = layout.peer_item_dir(envelope.id, data_dir)
    final.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".tmp-{envelope.id}-", dir=final.parent))
    try:
        (work / ENVELOPE_NAME).write_bytes(raw)
        (work / BODY_NAME).write_bytes(envelope.body.markdown.encode("utf-8"))
        (work / META_NAME).write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")
        if attachments:
            folder = work / ATTACHMENTS_NAME
            folder.mkdir()
            for name, source in attachments.items():
                shutil.move(str(source), folder / name)
        os.replace(work, final)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return final


@dataclass(frozen=True)
class StoredItem:
    """What the show and notice routes read from one inbox item."""

    envelope: ItemEnvelope
    meta: dict[str, Any]
    folder: Path

    def attachment_paths(self) -> list[Path]:
        folder = self.folder / ATTACHMENTS_NAME
        paths: list[Path] = []
        for attachment in self.envelope.body.attachments:
            path = folder / attachment.name
            if path.is_file():
                paths.append(path)
        return paths


def load_item(data_dir: Path | str, item_id: str) -> StoredItem | None:
    """Read one inbox item, or None when the id is bad or the item is missing."""
    try:
        folder = layout.peer_item_dir(item_id, data_dir)
    except ValueError:
        return None
    meta = read_json(folder / META_NAME)
    if meta is None:
        return None
    try:
        envelope = ItemEnvelope.model_validate_json((folder / ENVELOPE_NAME).read_bytes())
    except (OSError, ValueError):
        return None
    return StoredItem(envelope=envelope, meta=meta, folder=folder)
