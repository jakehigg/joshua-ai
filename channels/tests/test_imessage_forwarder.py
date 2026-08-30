"""The pipeline's own units: the coalescer, the staleness guard, and the drop
log. Pure where it can be (injected clock, no queue, no I/O).

Ported from the joshua/imessage bridge suite. ``submit()`` behaviour is
unchanged; the tests build the ``Forwarder`` (guard + core client + pipeline)
with offline fakes instead of the old httpx/respx core POST.
"""

from __future__ import annotations

import logging

import pytest
from imessage_helpers import FakeBB, FakeCore, cfg, load_data
from joshua_channels.adapters.imessage.forwarder import (
    Coalescer,
    Forwarder,
    coalesce_key,
    is_stale,
    merge_messages,
)
from joshua_channels.adapters.imessage.normalize import Attachment, InboundMessage
from joshua_channels.guard import Guard

NOW_S = 1_800_000_000.0


class FakeClock:
    """Monotonic-ish clock the test drives by hand."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _forwarder(**kwargs) -> Forwarder:
    settings = cfg()
    guard = Guard(settings, settings.channels.limits, config_provider=lambda: settings)
    return Forwarder(
        FakeBB(),  # type: ignore[arg-type]
        guard=guard,
        core_client=FakeCore(),  # type: ignore[arg-type]
        settings_provider=lambda: settings,
        **kwargs,
    )


def msg(
    guid: str,
    text: str = "",
    *,
    chat: str = "iMessage;-;+15551234567",
    sender: str = "+15551234567",
    attachments: tuple[Attachment, ...] = (),
    date_created: int = 1754300000000,
) -> InboundMessage:
    return InboundMessage(
        message_guid=guid,
        chat_guid=chat,
        is_group=False,
        sender_address=sender,
        sender_service="iMessage",
        text=text,
        chat_display_name=None,
        date_created=date_created,
        attachments=attachments,
    )


def att(index: int, name: str) -> Attachment:
    return Attachment(index=index, filename=name, mime_type=None, size=1, guid=f"g-{name}")


# ── merge_messages (pure) ────────────────────────────────────────────────────


def test_merge_keeps_the_first_guid_and_joins_oldest_first():
    merged = merge_messages(msg("A", "hey"), msg("B", "https://example.com"))
    assert merged.message_guid == "A"  # core's idempotency key = the first
    assert merged.text == "hey\nhttps://example.com"
    assert merged.date_created == 1754300000000  # when the turn started


def test_merge_skips_empty_parts():
    assert merge_messages(msg("A", ""), msg("B", "just this")).text == "just this"
    assert merge_messages(msg("A", "just this"), msg("B", "")).text == "just this"
    assert merge_messages(msg("A", ""), msg("B", "")).text == ""


def test_merge_reindexes_attachments_contiguously():
    first = msg("A", "look", attachments=(att(0, "a.png"), att(1, "b.png")))
    later = msg("B", "and this", attachments=(att(0, "c.png"),))
    merged = merge_messages(first, later)
    assert [a.index for a in merged.attachments] == [0, 1, 2]
    assert [a.filename for a in merged.attachments] == ["a.png", "b.png", "c.png"]


# ── Coalescer (pure, injected clock) ─────────────────────────────────────────


def test_disabled_window_is_a_pass_through():
    c = Coalescer(0.0, clock=FakeClock())
    batch = c.add(msg("A", "hey"), source="webhook")
    assert batch is not None and batch.message.message_guid == "A"
    assert batch.guids == ["A"] and batch.merged is False
    assert len(c) == 0


def test_two_messages_in_the_window_merge_into_one_batch():
    clock = FakeClock()
    c = Coalescer(2.0, clock=clock)
    assert c.add(msg("A", "hey"), source="webhook") is None
    clock.advance(0.25)
    assert c.add(msg("B", "https://example.com"), source="webhook") is None
    assert len(c) == 1

    clock.advance(2.0)
    (batch,) = c.pop_due()
    assert batch.message.message_guid == "A"
    assert batch.message.text == "hey\nhttps://example.com"
    assert batch.guids == ["A", "B"]
    assert batch.merged is True
    assert len(c) == 0


def test_each_arrival_restarts_the_window():
    clock = FakeClock()
    c = Coalescer(2.0, clock=clock)
    c.add(msg("A", "one"), source="webhook")
    clock.advance(1.5)
    c.add(msg("B", "two"), source="webhook")
    clock.advance(1.0)  # 2.5s since A, but only 1.0s since B
    assert c.pop_due() == []
    clock.advance(1.0)
    assert len(c.pop_due()) == 1


def test_different_chats_get_independent_buffers():
    clock = FakeClock()
    c = Coalescer(2.0, clock=clock)
    c.add(msg("A", "one", chat="iMessage;-;+1111"), source="webhook")
    clock.advance(1.0)
    c.add(msg("B", "two", chat="iMessage;-;+2222"), source="webhook")
    assert len(c) == 2

    clock.advance(1.5)  # A's window is up, B's is not
    due = c.pop_due()
    assert [b.message.message_guid for b in due] == ["A"]
    assert len(c) == 1


def test_two_senders_in_one_group_are_not_spliced_together():
    c = Coalescer(2.0, clock=FakeClock())
    group = "iMessage;+;chat100000000000000001"
    c.add(msg("A", "mine", chat=group, sender="a@example.com"), source="webhook")
    c.add(msg("B", "theirs", chat=group, sender="b@example.com"), source="webhook")
    assert len(c) == 2


def test_pop_all_returns_oldest_deadline_first():
    clock = FakeClock()
    c = Coalescer(2.0, clock=clock)
    c.add(msg("A", "one", chat="iMessage;-;+1111"), source="webhook")
    clock.advance(1.0)
    c.add(msg("B", "two", chat="iMessage;-;+2222"), source="webhook")
    assert [b.message.message_guid for b in c.pop_all()] == ["A", "B"]
    assert len(c) == 0


def test_coalesce_key_is_chat_plus_sender():
    assert coalesce_key(msg("A")) == ("iMessage;-;+15551234567", "+15551234567")


# ── staleness (pure) ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "date_created_ms,max_age_s,expected",
    [
        (int((NOW_S - 10) * 1000), 900.0, False),  # fresh
        (int((NOW_S - 899) * 1000), 900.0, False),  # inside the window
        (int((NOW_S - 27000) * 1000), 900.0, True),  # the 7.5h replay
        (int((NOW_S - 27000) * 1000), 0.0, False),  # guard disabled
        (0, 900.0, False),  # unknown is not old
        (int((NOW_S + 60) * 1000), 900.0, False),  # clock skew, future
    ],
)
def test_is_stale(date_created_ms, max_age_s, expected):
    assert is_stale(date_created_ms, now_s=NOW_S, max_age_s=max_age_s) is expected


# ── the drop log (this is what diagnoses webhook loss in prod) ───────────────


def drop_records(caplog) -> list[dict]:
    return [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("message") == "inbound dropped"
    ]


def test_every_drop_reason_logs_one_info_line(caplog: pytest.LogCaptureFixture):
    """Dropped-on-purpose and lost-in-transit look identical in prod unless the
    drop says so out loud."""
    caplog.set_level(logging.INFO, logger="channels.imessage.forwarder")
    fwd = _forwarder(stale_max_age_s=900.0, clock=lambda: NOW_S)

    assert fwd.submit(load_data("from_me_group"), source="webhook") == "dropped:from_me"
    assert fwd.submit(load_data("tapback"), source="webhook") == "dropped:tapback"
    assert fwd.submit(load_data("no_chats"), source="webhook") == "dropped:no_chat"

    empty = load_data("dm_text")
    empty["text"] = "   "
    assert fwd.submit(empty, source="webhook") == "dropped:no_content"

    # dm_text's fixture epoch is years before NOW_S → the replay guard.
    assert fwd.submit(load_data("dm_text"), source="webhook") == "dropped:stale"

    fresh = load_data("dm_text")
    fresh["dateCreated"] = int(NOW_S * 1000)
    assert fwd.submit(fresh, source="webhook") == "queued"
    assert fwd.submit(fresh, source="reconcile") == "dropped:duplicate"

    records = drop_records(caplog)
    assert [r["reason"] for r in records] == [
        "from_me",
        "tapback",
        "no_chat",
        "no_content",
        "stale",
        "duplicate",
    ]
    assert records[-1]["message_guid"] == "AABBCCDD-1111-2222-3333-444455556666"
    assert records[-1]["source"] == "reconcile"
    assert fwd.dropped == 6


def test_stale_drop_happens_before_dedupe_records_the_guid():
    """A dropped-as-stale guid must not poison the cache: if the same message
    arrives again legitimately fresh, it should still get through."""
    fwd = _forwarder(stale_max_age_s=900.0, clock=lambda: NOW_S)
    assert fwd.submit(load_data("dm_text"), source="webhook") == "dropped:stale"

    fresh = load_data("dm_text")
    fresh["dateCreated"] = int(NOW_S * 1000)
    assert fwd.submit(fresh, source="webhook") == "queued"


def test_coalescing_without_an_event_loop_queues_immediately():
    """There is no timer to arm outside a loop — hold nothing rather than hold
    it forever."""
    fwd = _forwarder(coalesce_window_s=2.0)
    assert fwd.submit(load_data("dm_text"), source="webhook") == "queued"
    assert fwd.pending == 0
    assert fwd.depth == 1


def test_stale_guard_is_off_when_unset():
    fwd = _forwarder()  # stale_max_age_s defaults to 0
    assert fwd.submit(load_data("dm_text"), source="webhook") == "queued"
