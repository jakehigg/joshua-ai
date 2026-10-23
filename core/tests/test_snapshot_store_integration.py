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


async def test_snapshot_times_gives_the_latest_good_fetch(db, repo) -> None:
    store = MemoryStore(db.pool)
    await repo.save_snapshot_success("open-tasks", None, AT, "A", "s1")
    await repo.save_snapshot_success("open-tasks", None, AT + timedelta(days=7), "A", "s1")
    await repo.save_snapshot_failure("open-tasks", None, "down")
    await repo.save_snapshot_failure("never-good", None, "down")
    times = await store.snapshot_times(["open-tasks", "never-good", "absent"])
    assert times == {"open-tasks": AT + timedelta(days=7)}
    assert await store.snapshot_times([]) == {}


async def test_kb_near_snapshots_gives_the_closest_items_of_the_named_snapshots(db) -> None:
    store = MemoryStore(db.pool)

    def vec(*head: float) -> list[float]:
        return [*head, *([0.0] * (384 - len(head)))]

    rows = (
        ("open-tasks", "snapshot/open-tasks/a", vec(1.0)),
        ("open-tasks", "snapshot/open-tasks/b", vec(0.8, 0.6)),
        ("open-tasks", "snapshot/open-tasks/c", vec(0.0, 1.0)),
        ("other", "snapshot/other/a", vec(1.0)),
    )
    for name, path, vector in rows:
        await store.kb_replace_item(
            source="snapshots",
            person_id=None,
            path=path,
            kind="snapshot",
            title=name,
            provenance="external",
            file_sha256=path,
            file_mtime=AT,
            doc_date=AT.date(),
            chunks=[("", f"{name}:\nitem {path}", vector)],
        )
    got = await store.kb_near_snapshots([vec(1.0)], names=["open-tasks"], min_sim=0.5, limit=5)
    assert [c.path for c in got] == ["snapshot/open-tasks/a", "snapshot/open-tasks/b"]
    assert got[0].similarity == pytest.approx(1.0)
    assert got[1].similarity == pytest.approx(0.8)
    assert all(c.kind == "snapshot" and c.title == "open-tasks" for c in got)
    one = await store.kb_near_snapshots([vec(1.0)], names=["open-tasks"], min_sim=0.5, limit=1)
    assert [c.path for c in one] == ["snapshot/open-tasks/a"]
    assert await store.kb_near_snapshots([], names=["open-tasks"], min_sim=0.5, limit=5) == []
    assert await store.kb_near_snapshots([vec(1.0)], names=[], min_sim=0.5, limit=5) == []
