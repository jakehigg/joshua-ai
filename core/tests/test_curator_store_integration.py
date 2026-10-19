"""The curator's store methods against a real Postgres: the `curator_finding`
rows of `Repo`, and `MemoryStore.kb_mean_vectors`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from joshua_core.memory.store import MemoryStore
from joshua_core.store.repo import FINDING_STATUSES

pytestmark = pytest.mark.integration

SEEN = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _vec(*head: float) -> list[float]:
    return [*head, *([0.0] * (384 - len(head)))]


async def _upsert(repo, fingerprint: str = "fp1", status: str = "open", seen=SEEN, run_id=1):
    return await repo.upsert_finding(
        run_id=run_id,
        kind="broken-link",
        tier="C",
        fingerprint=fingerprint,
        paths=["garden/roses.md"],
        detail={"target": "gone.md"},
        patch=None,
        status=status,
        seen_at=seen,
    )


async def test_a_finding_seen_again_keeps_first_seen_and_status(repo) -> None:
    first = await _upsert(repo)
    assert first["status"] == "open"
    assert await repo.set_finding_status(
        first["id"], "dismissed", at=SEEN, resolved_by="alex", note="fine"
    )
    again = await _upsert(repo, seen=SEEN + timedelta(days=1), run_id=2)
    assert again["detail"] == {"target": "gone.md", "note": "fine"}
    assert again["id"] == first["id"]
    assert again["status"] == "dismissed"
    assert again["first_seen"] == SEEN
    assert again["last_seen"] == SEEN + timedelta(days=1)
    assert again["run_id"] == 2


async def test_a_stale_finding_seen_again_is_open(repo) -> None:
    row = await _upsert(repo)
    await repo.set_finding_status(row["id"], "stale", at=SEEN)
    again = await _upsert(repo, seen=SEEN + timedelta(days=1))
    assert again["status"] == "open"
    assert again["resolved_at"] is None


async def test_an_applied_finding_records_the_curator(repo) -> None:
    row = await _upsert(repo, status="applied")
    assert row["resolved_by"] == "curator"
    assert row["resolved_at"] == SEEN


async def test_list_get_and_set_status(repo) -> None:
    a = await _upsert(repo, "a")
    b = await _upsert(repo, "b")
    assert [r["id"] for r in await repo.list_findings("open", 10)] == [b["id"], a["id"]]
    assert await repo.set_finding_status(a["id"], "dismissed", at=SEEN, note="not a problem")
    got = await repo.get_finding(a["id"])
    assert got["status"] == "dismissed"
    assert got["detail"] == {"target": "gone.md", "note": "not a problem"}
    assert [r["id"] for r in await repo.list_findings("open", 10)] == [b["id"]]
    assert len(await repo.list_findings(None, 10)) == 2
    assert await repo.get_finding(999) is None
    assert await repo.set_finding_status(999, "open", at=SEEN) is False
    assert set(FINDING_STATUSES) == {"open", "applied", "dismissed", "stale"}
    with pytest.raises(ValueError):
        await repo.list_findings("bogus", 10)


async def test_kb_mean_vectors_is_the_mean_of_the_chunks(db) -> None:
    store = MemoryStore(db.pool)
    await store.kb_replace_item(
        source="files",
        person_id=None,
        path="wiki/garden/roses.md",
        kind="wiki",
        title="Roses",
        provenance="own",
        file_sha256="sha",
        file_mtime=SEEN,
        doc_date=None,
        chunks=[("", "a", _vec(1.0)), ("", "b", _vec(0.0, 1.0))],
    )
    got = await store.kb_mean_vectors(["wiki/garden/roses.md", "wiki/none.md"])
    assert list(got) == ["wiki/garden/roses.md"]
    assert got["wiki/garden/roses.md"][:3] == pytest.approx([0.5, 0.5, 0.0])
    assert await store.kb_mean_vectors([]) == {}
