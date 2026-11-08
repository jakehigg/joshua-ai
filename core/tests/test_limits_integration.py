"""The `limits_snapshot` methods of `Repo` against a real Postgres: one row,
replaced on each save."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_save_and_load_the_limits_snapshot(repo) -> None:
    assert await repo.load_limits_snapshot() is None
    first = {"observed_at": "2026-11-08T01:00:00+00:00", "five_hour": None, "seven_day": None}
    await repo.save_limits_snapshot(first)
    assert await repo.load_limits_snapshot() == first
    second = {"observed_at": "2026-11-08T01:05:00+00:00", "five_hour": None, "seven_day": None}
    await repo.save_limits_snapshot(second)
    assert await repo.load_limits_snapshot() == second
