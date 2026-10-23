"""The `snapshot_result` methods of `Repo`, the task list of one person, and
the person rule of `MemoryStore.kb_search` for a snapshot row, against a
real Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from joshua_core.memory.store import MemoryStore

pytestmark = pytest.mark.integration

AT = datetime(2026, 3, 14, 15, 0, tzinfo=UTC)


def _vec() -> list[float]:
    return [1.0, *([0.0] * 383)]


async def test_a_success_then_failures_keep_the_good_text(repo) -> None:
    assert await repo.get_snapshot_result("open-tasks") is None
    await repo.save_snapshot_success("open-tasks", "alex", AT, "good", "sha1")
    assert await repo.save_snapshot_failure("open-tasks", "alex", "gateway 502") == 1
    assert await repo.save_snapshot_failure("open-tasks", "alex", "timeout") == 2
    row = await repo.get_snapshot_result("open-tasks")
    assert row["text"] == "good"
    assert row["sha256"] == "sha1"
    assert row["fetched_at"] == AT
    assert row["ok"] is False
    assert row["failures"] == 2
    assert row["last_error"] == "timeout"
    await repo.save_snapshot_success("open-tasks", "alex", AT + timedelta(minutes=5), "new", "s2")
    row = await repo.get_snapshot_result("open-tasks")
    assert (row["text"], row["failures"], row["last_error"], row["ok"]) == ("new", 0, None, True)


async def test_a_first_failure_has_no_text(repo) -> None:
    assert await repo.save_snapshot_failure("never-good", None, "down") == 1
    rows = await repo.list_snapshot_results()
    assert [(r["name"], r["text"], r["fetched_at"]) for r in rows] == [("never-good", None, None)]


async def test_the_tasks_of_one_person(repo) -> None:
    await repo.upsert_person("alex", "Alex")
    await repo.upsert_channel("cli:alex", "cli")
    await repo.upsert_channel("telegram:-1", "telegram", session_mode="shared")
    mine = await repo.get_or_create_conversation("cli:alex", "alex")
    group = await repo.get_or_create_conversation("telegram:-1", None)
    await repo.create_task(mine.id, "Check the backup", AT)
    done = await repo.create_task(mine.id, "Old task", AT)
    await repo.complete_task(done.id)
    await repo.create_task(group.id, "Group reminder", AT)
    assert [t.prompt for t in await repo.list_tasks_for_person("alex")] == ["Check the backup"]
    assert [t.prompt for t in await repo.list_tasks_for_person(None)] == ["Group reminder"]


async def test_a_person_snapshot_row_is_found_only_for_that_person(db) -> None:
    store = MemoryStore(db.pool)
    for person, path in (("alex", "snapshot/mine/1"), (None, "snapshot/shared/1")):
        await store.kb_replace_item(
            source="snapshots",
            person_id=person,
            path=path,
            kind="snapshot",
            title="t",
            provenance="external",
            file_sha256="s",
            file_mtime=AT,
            doc_date=AT.date(),
            chunks=[("", "item", _vec())],
        )

    async def paths(person: str | None) -> set[str]:
        return {c.path for c in await store.kb_search(person, _vec(), k=10, min_sim=0.0)}

    assert await paths("alex") == {"snapshot/mine/1", "snapshot/shared/1"}
    assert await paths("mia") == {"snapshot/shared/1"}
    assert await paths(None) == {"snapshot/shared/1"}
