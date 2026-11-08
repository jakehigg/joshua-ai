"""The `limits_snapshot` methods of `Repo` against a real Postgres: one row,
replaced on each save."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_save_and_load_the_limits_snapshot(repo) -> None:
    assert await repo.load_limits_snapshot() is None
    first = {"source": "usage_api", "observed_at": "2026-11-08T01:00:00+00:00"}
    await repo.save_limits_snapshot(first)
    assert await repo.load_limits_snapshot() == first
    second = {"source": "sdk_event", "observed_at": "2026-11-08T01:05:00+00:00"}
    await repo.save_limits_snapshot(second)
    assert await repo.load_limits_snapshot() == second
