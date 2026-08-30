"""The reconciliation poller: the pull backstop for lost webhooks."""

from __future__ import annotations

from typing import Any

import pytest
from imessage_helpers import FakeBB
from joshua_channels.adapters.imessage.bb_client import BlueBubblesError
from joshua_channels.adapters.imessage.reconcile import (
    Reconciler,
    date_created_ms,
    newer_than,
    next_watermark,
)


def row(guid: str, date_created: int) -> dict[str, Any]:
    return {"guid": guid, "dateCreated": date_created, "text": "hi"}


class FakeForwarder:
    """Records what the reconciler submits, and the status it returns."""

    def __init__(self, status: str = "queued") -> None:
        self.status = status
        self.calls: list[tuple[str, str]] = []

    def submit(self, data: Any, *, source: str) -> str:
        self.calls.append((data.get("guid"), source))
        return self.status


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_date_created_ms_coerces_and_defaults_zero():
    assert date_created_ms({"dateCreated": 123}) == 123
    assert date_created_ms({"dateCreated": "123"}) == 123
    assert date_created_ms({"dateCreated": None}) == 0
    assert date_created_ms({"dateCreated": True}) == 0
    assert date_created_ms({}) == 0


def test_newer_than_filters_on_the_watermark():
    rows = [row("A", 100), row("B", 200), row("C", 50)]
    assert [r["guid"] for r in newer_than(rows, 100)] == ["B"]


def test_next_watermark_never_moves_backwards():
    assert next_watermark([row("A", 100), row("B", 300)], 200) == 300
    assert next_watermark([], 200) == 200


# ── tick ─────────────────────────────────────────────────────────────────────


async def test_tick_submits_fresh_rows_oldest_first_and_advances_watermark():
    # BB returns DESC (newest first); the reconciler must submit oldest first.
    bb = FakeBB(rows=[row("B", 200), row("A", 100)])
    fwd = FakeForwarder()
    rec = Reconciler(bb, fwd, watermark_ms=0)  # type: ignore[arg-type]

    submitted = await rec.tick()

    assert submitted == 2
    assert [guid for guid, _ in fwd.calls] == ["A", "B"]  # ASC order to core
    assert all(source == "reconcile" for _, source in fwd.calls)
    assert rec.watermark_ms == 200
    assert rec.recovered == 2


async def test_tick_ignores_rows_at_or_below_the_watermark():
    bb = FakeBB(rows=[row("A", 100), row("B", 200)])
    fwd = FakeForwarder()
    rec = Reconciler(bb, fwd, watermark_ms=150)  # type: ignore[arg-type]

    submitted = await rec.tick()

    assert submitted == 1
    assert [guid for guid, _ in fwd.calls] == ["B"]
    assert rec.watermark_ms == 200


async def test_tick_does_not_count_a_dropped_row_as_recovered():
    bb = FakeBB(rows=[row("A", 100)])
    fwd = FakeForwarder(status="dropped:duplicate")
    rec = Reconciler(bb, fwd, watermark_ms=0)  # type: ignore[arg-type]

    await rec.tick()

    assert rec.recovered == 0
    assert rec.watermark_ms == 100  # watermark still advances


async def test_tick_swallows_a_query_error_and_keeps_the_watermark():
    class AngryBB(FakeBB):
        async def query_messages_since(self, after_ms: int, *, limit: int = 100):
            raise BlueBubblesError("mac asleep")

    rec = Reconciler(AngryBB(), FakeForwarder(), watermark_ms=42)  # type: ignore[arg-type]
    assert await rec.tick() == 0
    assert rec.watermark_ms == 42
    assert rec.ticks == 1


async def test_tick_warns_when_the_page_is_full(caplog: pytest.LogCaptureFixture):
    bb = FakeBB(rows=[row(f"g{i}", 100 + i) for i in range(3)])
    rec = Reconciler(bb, FakeForwarder(), limit=3, watermark_ms=0)  # type: ignore[arg-type]
    with caplog.at_level("WARNING", logger="channels.imessage.reconcile"):
        await rec.tick()
    assert any(
        isinstance(r.msg, dict) and "page was full" in r.msg.get("message", "")
        for r in caplog.records
    )


def test_enabled_needs_an_interval_and_a_configured_bb():
    on = Reconciler(FakeBB(), FakeForwarder(), interval_s=60)  # type: ignore[arg-type]
    assert on.enabled is True
    off = Reconciler(FakeBB(), FakeForwarder(), interval_s=0)  # type: ignore[arg-type]
    assert off.enabled is False
