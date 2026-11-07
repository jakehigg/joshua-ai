"""The peer outbox: send order, backoff, expiry, acks, prune, and the retry loop."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from joshua_channels.peers import outbox as outbox_module
from joshua_channels.peers.outbox import Outbox, OutboxExists, backoff, retry_loop
from joshua_channels.peers.protocol import ACK_MIN_TTL, make_ack
from joshua_shared.peers import AckEnvelope, Attachment, ItemEnvelope, parse_envelope
from peer_fakes import NOW, FakeClock, ack, item, sha, ulid


class Sender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, str]] = []
        self.fail = False

    async def __call__(self, chat_id: str, data: bytes, filename: str) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((chat_id, data, filename))


def _rig(tmp_path: Path, *, no_sender: bool = False) -> tuple[Outbox, Sender, FakeClock]:
    sender = Sender()
    clock = FakeClock()
    box = Outbox(tmp_path, sender=lambda: None if no_sender else sender, clock=clock)
    return box, sender, clock


def _envelope(item_id: str, **kwargs) -> ItemEnvelope:  # type: ignore[no-untyped-def]
    return ItemEnvelope.model_validate(item(item_id, sender="alex", **kwargs))


def _ack(ref: str, status: str = "accepted") -> AckEnvelope:
    parsed = AckEnvelope.model_validate(ack(ulid(99), ref, status))
    return parsed


def test_backoff_doubles_up_to_the_cap() -> None:
    assert backoff(1) == timedelta(seconds=60)
    assert backoff(2) == timedelta(seconds=120)
    assert backoff(3) == timedelta(seconds=240)
    assert backoff(20) == timedelta(seconds=outbox_module.RETRY_MAX_S)


async def test_an_attempt_sends_the_attachments_first_and_the_envelope_last(
    tmp_path: Path,
) -> None:
    box, sender, _ = _rig(tmp_path)
    one, two = b"first file", b"second file"
    sources = []
    for name, data in (("a.jpg", one), ("b.jpg", two)):
        path = tmp_path / name
        path.write_bytes(data)
        sources.append(path)
    attachments = [
        {"name": "a.jpg", "mime": "image/jpeg", "sha256": sha(one), "size": len(one)},
        {"name": "b.jpg", "mime": "image/jpeg", "sha256": sha(two), "size": len(two)},
    ]
    envelope = _envelope(ulid(1), attachments=attachments)
    files = [(Attachment.model_validate(a), p) for a, p in zip(attachments, sources, strict=True)]
    box.enqueue("rivera", "900", envelope, files)
    # The source may change after the enqueue; the outbox holds its own copy.
    sources[0].write_bytes(b"changed")

    assert await box.attempt(ulid(1)) is True

    assert [(c, f) for c, _, f in sender.sent] == [
        ("900", "a.jpg"),
        ("900", "b.jpg"),
        ("900", "envelope.json"),
    ]
    assert sender.sent[0][1] == one
    assert parse_envelope(sender.sent[2][1]).id == ulid(1)
    state = box.state(ulid(1))
    assert state is not None
    assert state["attempts"] == 1
    assert state["next_attempt_at"] == (NOW + timedelta(seconds=60)).isoformat()
    assert state["status"] == "pending"


async def test_a_duplicate_id_is_refused(tmp_path: Path) -> None:
    box, _, _ = _rig(tmp_path)
    box.enqueue("rivera", "900", _envelope(ulid(1)), [])
    with pytest.raises(OutboxExists):
        box.enqueue("rivera", "900", _envelope(ulid(1)), [])


async def test_retry_waits_for_the_backoff_then_sends_again(tmp_path: Path) -> None:
    box, sender, clock = _rig(tmp_path)
    box.enqueue("rivera", "900", _envelope(ulid(1)), [])

    assert await box.retry_due() == 1
    assert await box.retry_due() == 0
    clock.advance(seconds=59)
    assert await box.retry_due() == 0
    clock.advance(seconds=1)
    assert await box.retry_due() == 1
    state = box.state(ulid(1))
    assert state is not None and state["attempts"] == 2
    assert state["next_attempt_at"] == (clock.now + timedelta(seconds=120)).isoformat()
    assert len(sender.sent) == 2


async def test_a_pending_item_expires_with_no_further_send(tmp_path: Path) -> None:
    box, sender, clock = _rig(tmp_path)
    box.enqueue("rivera", "900", _envelope(ulid(1), expires_at=NOW + timedelta(minutes=5)), [])
    await box.retry_due()
    clock.advance(minutes=5)

    assert await box.retry_due() == 0

    state = box.state(ulid(1))
    assert state is not None and state["status"] == "expired"
    assert len(sender.sent) == 1
    assert await box.attempt(ulid(1)) is False


async def test_a_failed_send_counts_and_stays_pending(tmp_path: Path) -> None:
    box, sender, _ = _rig(tmp_path)
    sender.fail = True
    box.enqueue("rivera", "900", _envelope(ulid(1)), [])

    assert await box.attempt(ulid(1)) is False
    state = box.state(ulid(1))
    assert state is not None
    assert (state["attempts"], state["status"]) == (1, "pending")


async def test_no_telegram_counts_the_attempt(tmp_path: Path) -> None:
    box, _, _ = _rig(tmp_path, no_sender=True)
    box.enqueue("rivera", "900", _envelope(ulid(1)), [])

    assert await box.attempt(ulid(1)) is False
    state = box.state(ulid(1))
    assert state is not None and state["attempts"] == 1


async def test_an_unknown_id_is_not_sent(tmp_path: Path) -> None:
    box, sender, _ = _rig(tmp_path)
    assert await box.attempt(ulid(1)) is False
    assert sender.sent == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [("accepted", "acked"), ("unmatched", "acked"), ("rejected", "failed")],
)
def test_an_ack_settles_the_item(tmp_path: Path, status: str, expected: str) -> None:
    box, _, _ = _rig(tmp_path)
    box.enqueue("rivera", "900", _envelope(ulid(1)), [])

    assert box.apply_ack("rivera", _ack(ulid(1), status)) is True

    state = box.state(ulid(1))
    assert state is not None
    assert (state["status"], state["ack_status"]) == (expected, status)


def test_a_second_ack_changes_nothing(tmp_path: Path) -> None:
    box, _, _ = _rig(tmp_path)
    box.enqueue("rivera", "900", _envelope(ulid(1)), [])
    box.apply_ack("rivera", _ack(ulid(1), "accepted"))

    assert box.apply_ack("rivera", _ack(ulid(1), "rejected")) is False
    state = box.state(ulid(1))
    assert state is not None and state["ack_status"] == "accepted"


def test_an_ack_from_another_peer_or_for_an_unknown_ref_is_dropped(tmp_path: Path) -> None:
    box, _, _ = _rig(tmp_path)
    box.enqueue("rivera", "900", _envelope(ulid(1)), [])

    assert box.apply_ack("quiet", _ack(ulid(1))) is False
    assert box.apply_ack("rivera", _ack(ulid(2))) is False
    state = box.state(ulid(1))
    assert state is not None and state["status"] == "pending"


async def test_an_acked_item_is_not_sent_again(tmp_path: Path) -> None:
    box, sender, clock = _rig(tmp_path)
    box.enqueue("rivera", "900", _envelope(ulid(1)), [])
    await box.retry_due()
    box.apply_ack("rivera", _ack(ulid(1)))
    clock.advance(hours=2)

    assert await box.retry_due() == 0
    assert len(sender.sent) == 1


async def test_prune_removes_a_settled_item_after_it_expires(tmp_path: Path) -> None:
    box, _, clock = _rig(tmp_path)
    box.enqueue("rivera", "900", _envelope(ulid(1), expires_at=NOW + timedelta(hours=1)), [])
    box.enqueue("rivera", "900", _envelope(ulid(2), expires_at=NOW + timedelta(hours=1)), [])
    box.apply_ack("rivera", _ack(ulid(1)))

    assert box.prune() == 0
    clock.advance(hours=1)
    assert box.prune() == 1
    assert box.ids() == [ulid(2)]


def test_an_ack_lives_at_least_as_long_as_its_item() -> None:
    later = NOW + timedelta(days=30)
    long_ack = parse_envelope(make_ack(ulid(1), "accepted", NOW, later))
    short_ack = parse_envelope(make_ack(ulid(1), "accepted", NOW, NOW + timedelta(minutes=1)))
    assert long_ack.expires_at == later
    assert short_ack.expires_at == NOW + ACK_MIN_TTL


async def test_the_retry_loop_survives_a_failed_tick_and_stops_on_cancel() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    async def tick() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await retry_loop(tick, interval_s=7, sleep=sleep)
    assert len(calls) == 3
    assert sleeps == [7, 7, 7]
