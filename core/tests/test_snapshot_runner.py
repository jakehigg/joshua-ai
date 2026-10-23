"""The snapshot runner: cadence, failures, the last good text, and reconcile.

An injected clock and a zero jitter drive the cadence. A fake store keeps the
`snapshot_result` rows in memory, and a fake indexer counts the reconciles.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
from joshua_core.snapshots import (
    Fetched,
    GatewayFetcher,
    SchedulingFetcher,
    SnapshotRunner,
    build_runner,
)
from joshua_shared.config import Snapshot
from joshua_shared.http import FleetClient

START = datetime(2026, 3, 14, 15, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


class Store:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def list_snapshot_results(self) -> list[dict]:
        return list(self.rows.values())

    async def get_snapshot_result(self, name: str) -> dict | None:
        return self.rows.get(name)

    async def save_snapshot_success(self, name, person_id, fetched_at, text, sha256) -> None:
        self.rows[name] = {
            "name": name,
            "person_id": person_id,
            "fetched_at": fetched_at,
            "ok": True,
            "text": text,
            "sha256": sha256,
            "failures": 0,
            "last_error": None,
        }

    async def save_snapshot_failure(self, name, person_id, error) -> int:
        row = self.rows.setdefault(
            name,
            {
                "name": name,
                "person_id": person_id,
                "fetched_at": None,
                "text": None,
                "sha256": None,
                "failures": 0,
            },
        )
        row.update(ok=False, failures=row["failures"] + 1, last_error=error)
        return row["failures"]


class Indexer:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def reindex(self, *, source=None):
        self.calls.append(source)
        return {}


class Fetcher:
    def __init__(self) -> None:
        self.answers: list[Fetched] = []
        self.calls: list[str] = []

    async def __call__(self, snap: Snapshot) -> Fetched:
        self.calls.append(snap.name)
        return self.answers.pop(0) if self.answers else Fetched(text="same text")


def _snap(**kw) -> Snapshot:
    base = {"name": "open-tasks", "server": "tasks", "tool": "list_items", "every": "5m"}
    return Snapshot(**{**base, **kw})


def _runner(snaps, *, fetcher=None, scheduling=None, clock=None):
    store, indexer = Store(), Indexer()
    runner = SnapshotRunner(
        snaps,
        store=store,
        indexer=indexer,
        gateway=fetcher,
        scheduling=scheduling,
        clock=clock or Clock(),
        jitter=lambda every: 0.0,
    )
    return runner, store, indexer


async def test_a_snapshot_runs_on_its_cadence() -> None:
    clock, fetcher = Clock(), Fetcher()
    runner, _, _ = _runner(
        [_snap(), _snap(name="hourly", every="1h")], fetcher=fetcher, clock=clock
    )
    assert set(await runner.tick()) == {"open-tasks", "hourly"}
    clock.advance(minutes=4)
    assert await runner.tick() == {}
    clock.advance(minutes=1)
    assert set(await runner.tick()) == {"open-tasks"}
    clock.advance(minutes=55)
    assert set(await runner.tick()) == {"open-tasks", "hourly"}


async def test_the_jitter_adds_to_the_wait() -> None:
    clock, fetcher = Clock(), Fetcher()
    runner = SnapshotRunner(
        [_snap()],
        store=Store(),
        indexer=Indexer(),
        gateway=fetcher,
        scheduling=None,
        clock=clock,
        jitter=lambda every: 30.0,
    )
    await runner.tick()
    clock.advance(minutes=5)
    assert await runner.tick() == {}
    clock.advance(seconds=30)
    assert await runner.tick() == {"open-tasks": "same"}


async def test_reconcile_runs_only_on_a_change() -> None:
    clock, fetcher = Clock(), Fetcher()
    runner, _, indexer = _runner([_snap()], fetcher=fetcher, clock=clock)
    fetcher.answers = [Fetched(text="A"), Fetched(text="A"), Fetched(text="B")]
    assert await runner.tick() == {"open-tasks": "changed"}
    assert indexer.calls == ["snapshots"]
    clock.advance(minutes=5)
    assert await runner.tick() == {"open-tasks": "same"}
    assert indexer.calls == ["snapshots"]
    clock.advance(minutes=5)
    assert await runner.tick() == {"open-tasks": "changed"}
    assert indexer.calls == ["snapshots", "snapshots"]


async def test_a_failure_keeps_the_last_good_text_and_counts() -> None:
    clock, fetcher = Clock(), Fetcher()
    runner, store, _ = _runner([_snap()], fetcher=fetcher, clock=clock)
    fetcher.answers = [Fetched(text="good"), Fetched(error="gateway 502"), Fetched(error="x")]
    await runner.tick()
    for _ in range(2):
        clock.advance(minutes=5)
        assert await runner.tick() == {"open-tasks": "failed"}
    row = store.rows["open-tasks"]
    assert row["text"] == "good"
    assert row["failures"] == 2
    assert row["last_error"] == "x"
    assert row["fetched_at"] == START
    clock.advance(minutes=5)
    fetcher.answers = [Fetched(text="good")]
    await runner.tick()
    assert store.rows["open-tasks"]["failures"] == 0


async def test_the_third_failure_takes_the_snapshot_out() -> None:
    clock, fetcher = Clock(), Fetcher()
    runner, _, indexer = _runner([_snap(every="1m", max_age="1h")], fetcher=fetcher, clock=clock)
    fetcher.answers = [Fetched(text="good")] + [Fetched(error="down")] * 3
    await runner.tick()
    for _ in range(2):
        clock.advance(minutes=1)
        await runner.tick()
    assert indexer.calls == ["snapshots"]
    clock.advance(minutes=1)
    await runner.tick()
    assert indexer.calls == ["snapshots", "snapshots"]


async def test_crossing_the_staleness_line_reconciles() -> None:
    clock, fetcher = Clock(), Fetcher()
    runner, _, indexer = _runner([_snap(every="1h", max_age="30m")], fetcher=fetcher, clock=clock)
    fetcher.answers = [Fetched(text="good")]
    await runner.tick()
    assert indexer.calls == ["snapshots"]
    clock.advance(minutes=20)
    await runner.tick()
    assert indexer.calls == ["snapshots"]
    clock.advance(minutes=11)
    await runner.tick()
    assert indexer.calls == ["snapshots", "snapshots"]


async def test_output_past_max_chars_is_cut(caplog) -> None:
    fetcher = Fetcher()
    runner, store, _ = _runner([_snap(max_chars=10)], fetcher=fetcher)
    fetcher.answers = [Fetched(text="0123456789abcdef")]
    await runner.tick()
    assert store.rows["open-tasks"]["text"] == "0123456789"


async def test_the_log_never_holds_the_text(caplog) -> None:
    fetcher = Fetcher()
    runner, _, _ = _runner([_snap()], fetcher=fetcher)
    fetcher.answers = [Fetched(text="private words")]
    with caplog.at_level("DEBUG"):
        await runner.tick()
    assert "private words" not in caplog.text


async def test_a_scheduling_snapshot_runs_in_core() -> None:
    task = SimpleNamespace(
        id="t1",
        status="active",
        cron_expr="0 9 * * 1",
        next_run_at=START,
        prompt="Remind Alex about the filter change",
    )

    class Tasks:
        seen: list = []

        async def list_tasks_for_person(self, person_id):
            self.seen.append(person_id)
            return [task]

    tasks = Tasks()
    gateway = Fetcher()
    snap = _snap(name="own-tasks", server="scheduling", tool="list_tasks", scope="alex")
    runner, store, _ = _runner([snap], fetcher=gateway, scheduling=SchedulingFetcher(tasks))
    await runner.tick()
    assert gateway.calls == []
    assert tasks.seen == ["alex"]
    items = json.loads(store.rows["own-tasks"]["text"])
    assert items[0]["instruction"] == "Remind Alex about the filter change"
    assert items[0]["schedule"] == "cron 0 9 * * 1"


def _gateway(handler) -> GatewayFetcher:
    client = FleetClient(
        "http://gateway:8080", "core-token", transport=httpx.MockTransport(handler)
    )
    return GatewayFetcher(client)


async def test_the_gateway_fetcher_posts_an_empty_body_with_the_bearer() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(
            path=request.url.path, auth=request.headers["authorization"], body=request.content
        )
        return httpx.Response(
            200, json={"ok": True, "text": "A", "is_error": False, "fetched_at": "t"}
        )

    fetched = await _gateway(handler)(_snap())
    assert fetched == Fetched(text="A")
    assert seen == {"path": "/v1/snapshots/open-tasks", "auth": "Bearer core-token", "body": b""}


async def test_the_gateway_fetcher_reports_a_refusal_and_a_tool_error() -> None:
    refused = await _gateway(lambda r: httpx.Response(403, json={"error": "tool_destructive"}))(
        _snap()
    )
    assert refused.error == "gateway 403: tool_destructive"
    tool_error = await _gateway(
        lambda r: httpx.Response(
            200, json={"ok": False, "text": "no", "is_error": True, "fetched_at": "t"}
        )
    )(_snap())
    assert tool_error.error == "tool error"


async def test_build_runner_has_no_gateway_without_a_url() -> None:
    runner = build_runner(
        [_snap()], store=Store(), indexer=Indexer(), gateway_url="", gateway_token=""
    )
    assert await runner.run_once(_snap()) == "failed"


async def test_start_and_stop() -> None:
    runner, _, _ = _runner([_snap()], fetcher=Fetcher())
    runner.start()
    await runner.stop()
    empty, _, _ = _runner([], fetcher=Fetcher())
    empty.start()
    await empty.stop()
