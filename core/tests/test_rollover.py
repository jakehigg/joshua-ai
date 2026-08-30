"""Offline tests for the daily session rollover after the nightly run."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from joshua_core.engine.cron import now_in
from joshua_core.memory.nightly import NightlyReflector
from joshua_core.store.models import Person
from nightly_fakes import FakeIndexer, FakeManager, FakeReflectRepo, canned_oneshot, make_settings

PEOPLE = [Person(id="alex", display_name="Alex")]
TODAY = now_in("America/New_York").date()
OLD = date(2020, 1, 1)


def _reflector(repo: FakeReflectRepo, tmp_path: Path, *, extra: str = "") -> tuple:
    indexer, manager = FakeIndexer(), FakeManager()
    reflector = NightlyReflector(
        repo=repo,
        indexer=indexer,
        manager=manager,
        settings=make_settings(extra),
        data_dir=tmp_path,
        oneshot=canned_oneshot(),
    )
    return reflector, manager


async def test_run_once_rolls_over_and_flushes(tmp_path: Path) -> None:
    conversations = {
        "c1": {"session_started_on": OLD, "sdk_session_id": "old"},
        "c2": {"session_started_on": None, "sdk_session_id": "x"},
    }
    repo = FakeReflectRepo(people=PEOPLE, conversations=conversations)
    reflector, manager = _reflector(repo, tmp_path)

    summary = await reflector.run_once()

    assert summary["rolled_over"] == 2
    assert manager.flushed == 1
    assert conversations["c1"]["sdk_session_id"] is None
    assert conversations["c1"]["session_started_on"] == TODAY
    assert conversations["c2"]["sdk_session_id"] is None


async def test_rollover_leaves_today_sessions_alone(tmp_path: Path) -> None:
    conversations = {
        "fresh": {"session_started_on": TODAY, "sdk_session_id": "keep"},
        "stale": {"session_started_on": OLD, "sdk_session_id": "drop"},
    }
    repo = FakeReflectRepo(people=PEOPLE, conversations=conversations)
    reflector, manager = _reflector(repo, tmp_path)

    summary = await reflector.run_once()

    assert summary["rolled_over"] == 1
    assert conversations["fresh"]["sdk_session_id"] == "keep"
    assert conversations["stale"]["sdk_session_id"] is None
    assert manager.flushed == 1


async def test_rollover_disabled(tmp_path: Path) -> None:
    conversations = {"c1": {"session_started_on": OLD, "sdk_session_id": "old"}}
    repo = FakeReflectRepo(people=PEOPLE, conversations=conversations)
    reflector, manager = _reflector(repo, tmp_path, extra="\nmemory:\n  daily_rollover: false\n")

    summary = await reflector.run_once()

    assert "rolled_over" not in summary
    assert conversations["c1"]["sdk_session_id"] == "old"
    assert manager.flushed == 0


async def test_reflect_alone_does_not_roll_over(tmp_path: Path) -> None:
    conversations = {"c1": {"session_started_on": OLD, "sdk_session_id": "old"}}
    repo = FakeReflectRepo(people=PEOPLE, conversations=conversations)
    reflector, manager = _reflector(repo, tmp_path)

    await reflector.reflect()

    assert conversations["c1"]["sdk_session_id"] == "old"
    assert manager.flushed == 0
