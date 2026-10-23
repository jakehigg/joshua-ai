"""The snapshot runner: run each configured snapshot on its cadence.

A snapshot (``memory.snapshots``) is one fixed MCP tool call. One background
loop runs each snapshot on its ``every``, with a small jitter, and one at a
time. No model takes part.

- A gateway snapshot calls ``POST /v1/snapshots/{name}`` with the fleet
  bearer. The body is empty: the gateway reads the tool and the arguments from
  its own copy of the config.
- A snapshot on ``scheduling`` runs the listing tool of core's scheduling
  server in process, with no gateway.

The latest output goes in ``snapshot_result``. A failed call keeps the last
good text and adds one to ``failures``. When the output changes, or a
snapshot goes stale or fresh, the runner asks the indexer to reconcile the
``snapshots`` source. The log never holds the text.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from joshua_shared.config import Snapshot
from joshua_shared.contracts import SNAPSHOT_PATH
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger

from joshua_core.memory.sources.snapshots import SOURCE_NAME, is_listed

logger = get_logger("snapshots")

# How often the loop looks for a due snapshot. The shortest `every` is 1m.
TICK_SECONDS = 15.0
# At most this share of `every` is added to each wait, so two snapshots with
# the same cadence do not stay in step.
JITTER_SHARE = 0.1
# The gateway gives a snapshot call 30 seconds. Wait a little more.
GATEWAY_TIMEOUT_S = 45.0
# A last error is cut to this many characters.
ERROR_CHARS = 300


@dataclass(frozen=True)
class Fetched:
    """The outcome of one snapshot call. ``error`` is None for a good call."""

    text: str = ""
    error: str | None = None


class Fetcher(Protocol):
    async def __call__(self, snap: Snapshot) -> Fetched: ...


class SnapshotStore(Protocol):
    async def list_snapshot_results(self) -> list[dict[str, Any]]: ...

    async def get_snapshot_result(self, name: str) -> dict[str, Any] | None: ...

    async def save_snapshot_success(
        self, name: str, person_id: str | None, fetched_at: datetime, text: str, sha256: str
    ) -> None: ...

    async def save_snapshot_failure(self, name: str, person_id: str | None, error: str) -> int: ...


class Reindexer(Protocol):
    async def reindex(self, *, source: str | None = None) -> dict[str, Any]: ...


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class GatewayFetcher:
    """Run a gateway snapshot through ``POST /v1/snapshots/{name}``."""

    def __init__(self, client: FleetClient):
        self._client = client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __call__(self, snap: Snapshot) -> Fetched:
        try:
            response = await self._client.post_json(SNAPSHOT_PATH.format(name=snap.name))
        except Exception as exc:  # noqa: BLE001 — any transport fault is one failure
            return Fetched(error=f"gateway unreachable: {type(exc).__name__}")
        if response.status_code != 200:
            try:
                reason = str(response.json().get("error", ""))
            except ValueError:
                reason = ""
            return Fetched(error=f"gateway {response.status_code}: {reason}".strip())
        body = response.json()
        if not body.get("ok") or body.get("is_error"):
            return Fetched(error="tool error")
        return Fetched(text=str(body.get("text") or ""))


class TaskLister(Protocol):
    async def list_tasks_for_person(self, person_id: str | None) -> list[Any]: ...


class SchedulingFetcher:
    """Run the ``list_tasks`` tool of core's scheduling server in process.

    The output is a JSON list with one object per active or paused task of
    the scope's person, or of the conversations with no person for
    ``shared``.
    """

    def __init__(self, tasks: TaskLister):
        self._tasks = tasks

    async def __call__(self, snap: Snapshot) -> Fetched:
        try:
            tasks = await self._tasks.list_tasks_for_person(snap.person_id)
        except Exception as exc:  # noqa: BLE001 — a failed read is one failure
            return Fetched(error=f"task list failed: {type(exc).__name__}")
        items = [
            {
                "task": task.id,
                "status": task.status,
                "schedule": f"cron {task.cron_expr}" if task.cron_expr else "one-time",
                "next run": task.next_run_at.isoformat(),
                "instruction": task.prompt,
            }
            for task in tasks
        ]
        return Fetched(text=json.dumps(items, ensure_ascii=False))


class SnapshotRunner:
    """Run every snapshot on its cadence and keep the latest output."""

    def __init__(
        self,
        snapshots: Sequence[Snapshot],
        *,
        store: SnapshotStore,
        indexer: Reindexer,
        gateway: Fetcher | None,
        scheduling: Fetcher | None,
        clock: Callable[[], datetime] | None = None,
        jitter: Callable[[float], float] | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
    ):
        self._snapshots = list(snapshots)
        self._store = store
        self._indexer = indexer
        self._gateway = gateway
        self._scheduling = scheduling
        self._clock = clock or (lambda: datetime.now(UTC))
        self._jitter = jitter or (lambda every: random.uniform(0.0, every * JITTER_SHARE))  # noqa: S311
        self._sleep = sleep or asyncio.sleep
        # name -> the time the snapshot is next due. A new runner runs each
        # snapshot at the first tick.
        self._due: dict[str, datetime] = {}
        # name -> whether the source listed the snapshot at the last check.
        self._listed: dict[str, bool] = {}
        self._task: asyncio.Task[None] | None = None

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not self._snapshots:
            return
        self._task = asyncio.create_task(self._loop(), name="snapshot-runner")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        close = getattr(self._gateway, "aclose", None)
        if close is not None:
            await close()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a bad tick must not stop the loop
                logger.error({"message": "snapshot tick failed", "error": str(exc)})
            await self._sleep(TICK_SECONDS)

    # --- one tick ----------------------------------------------------------

    async def tick(self) -> dict[str, str]:
        """Run each due snapshot once, then reconcile the index if a snapshot
        changed or crossed its staleness line. Returns ``{name: outcome}`` for
        the snapshots that ran."""
        outcomes: dict[str, str] = {}
        changed = False
        for snap in self._snapshots:
            now = self._clock()
            due = self._due.get(snap.name)
            if due is not None and now < due:
                continue
            outcome = await self.run_once(snap)
            outcomes[snap.name] = outcome
            changed = changed or outcome == "changed"
            wait = snap.every_seconds + self._jitter(float(snap.every_seconds))
            self._due[snap.name] = now + timedelta(seconds=wait)
        if await self._listing_changed():
            changed = True
        if changed:
            await self._reconcile()
        return outcomes

    async def run_once(self, snap: Snapshot) -> str:
        """Call ``snap`` once and store the outcome: ``changed``, ``same``, or
        ``failed``."""
        fetcher = self._scheduling if snap.in_core else self._gateway
        if fetcher is None:
            fetched = Fetched(error="no fetcher for this server")
        else:
            fetched = await fetcher(snap)
        if fetched.error is not None:
            error = fetched.error[:ERROR_CHARS]
            failures = await self._store.save_snapshot_failure(snap.name, snap.person_id, error)
            logger.warning(
                {
                    "message": "snapshot failed",
                    "snapshot": snap.name,
                    "failures": failures,
                    "error": error,
                }
            )
            return "failed"
        text = fetched.text
        if len(text) > snap.max_chars:
            logger.warning(
                {
                    "message": "snapshot output cut",
                    "snapshot": snap.name,
                    "chars": len(text),
                    "max_chars": snap.max_chars,
                }
            )
            text = text[: snap.max_chars]
        sha = sha256_text(text)
        before = await self._store.get_snapshot_result(snap.name)
        await self._store.save_snapshot_success(snap.name, snap.person_id, self._clock(), text, sha)
        same = before is not None and before.get("sha256") == sha and before.get("text") is not None
        logger.info(
            {
                "message": "snapshot fetched",
                "snapshot": snap.name,
                "chars": len(text),
                "sha256": sha[:12],
                "changed": not same,
            }
        )
        return "same" if same else "changed"

    async def _listing_changed(self) -> bool:
        """True when a snapshot went stale or fresh since the last check."""
        rows = {row["name"]: row for row in await self._store.list_snapshot_results()}
        now = self._clock()
        moved = False
        for snap in self._snapshots:
            listed = is_listed(rows.get(snap.name), snap, now)
            if snap.name in self._listed and self._listed[snap.name] != listed:
                moved = True
            self._listed[snap.name] = listed
        return moved

    async def _reconcile(self) -> None:
        try:
            await self._indexer.reindex(source=SOURCE_NAME)
        except Exception as exc:  # noqa: BLE001 — the next change tries again
            logger.warning({"message": "snapshot reindex failed", "error": str(exc)})


def build_runner(
    snapshots: Sequence[Snapshot],
    *,
    store: Any,
    indexer: Reindexer,
    gateway_url: str,
    gateway_token: str,
) -> SnapshotRunner:
    """Wire the runner: the gateway client for a gateway snapshot, and the
    task list for a snapshot on ``scheduling``."""
    gateway: Fetcher | None = None
    if gateway_url and any(not s.in_core for s in snapshots):
        gateway = GatewayFetcher(FleetClient(gateway_url, gateway_token, GATEWAY_TIMEOUT_S))
    return SnapshotRunner(
        snapshots,
        store=store,
        indexer=indexer,
        gateway=gateway,
        scheduling=SchedulingFetcher(store),
    )
