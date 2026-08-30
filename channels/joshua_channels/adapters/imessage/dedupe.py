"""TTL dedupe on message guid.

The same message reaches the pipeline twice by design: once from the webhook
and again from the reconciliation poller (which re-reads a rolling window). BB
also re-delivers on some server-side edits. A bounded TTL cache is enough —
the two paths converge within seconds, and a guid older than the TTL can no
longer be in flight anywhere.

In-memory on purpose: a pod restart loses the set, and the reconciler's
watermark then re-reads a window that may duplicate what the pre-restart
process already forwarded. Core is the idempotency backstop for that narrow
case (it sees ``message_id`` on every turn).
"""

from __future__ import annotations

from cachetools import TTLCache


class GuidDedupe:
    """``seen()`` is check-and-record: True means "already handled, drop it"."""

    def __init__(self, maxsize: int = 4096, ttl: float = 900.0) -> None:
        self._cache: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl)

    def seen(self, guid: str | None) -> bool:
        """Record ``guid`` and report whether it was already present.

        An empty/absent guid can't be deduped — treat it as new so the message
        still gets through (a duplicate is better than a silent loss).
        """
        if not guid:
            return False
        if guid in self._cache:
            return True
        self._cache[guid] = True
        return False

    def record(self, guid: str | None) -> None:
        """Record without asking. Used by the coalescer: several guids merge
        into one forwarded message, and every constituent must count as handled
        even though only the first one names the result."""
        if guid:
            self._cache[guid] = True

    def __contains__(self, guid: str) -> bool:
        return guid in self._cache

    def __len__(self) -> int:
        return len(self._cache)
