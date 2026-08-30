"""The TTL guid dedupe cache."""

from __future__ import annotations

from joshua_channels.adapters.imessage.dedupe import GuidDedupe


def test_seen_is_check_and_record():
    d = GuidDedupe()
    assert d.seen("A") is False  # first sight
    assert d.seen("A") is True  # already handled
    assert "A" in d
    assert len(d) == 1


def test_empty_guid_is_never_deduped():
    d = GuidDedupe()
    assert d.seen("") is False
    assert d.seen(None) is False
    assert len(d) == 0


def test_record_marks_without_asking():
    d = GuidDedupe()
    d.record("B")
    assert d.seen("B") is True
    d.record("")  # empty is a no-op
    assert len(d) == 1


def test_ttl_expiry_forgets_a_guid():
    from cachetools import TTLCache

    ticks = [0.0]
    d = GuidDedupe(ttl=10.0)
    # Drive the cache clock by hand to avoid a real sleep.
    d._cache = TTLCache(maxsize=16, ttl=10.0, timer=lambda: ticks[0])
    assert d.seen("C") is False
    ticks[0] = 5.0
    assert d.seen("C") is True  # still inside the TTL
    ticks[0] = 20.0
    assert d.seen("C") is False  # expired → treated as new again
