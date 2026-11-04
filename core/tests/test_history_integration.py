"""The SQL of ``Repo.search_transcript`` and ``Repo.read_transcript`` on Postgres.

Each test proves the scope: a row of another conversation never comes back,
even when it matches the words or the window better.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from joshua_core.store.repo import Repo

pytestmark = pytest.mark.integration

MINE = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
T0 = datetime(2026, 3, 10, 14, 0, tzinfo=UTC)


async def _add(
    repo: Repo,
    conversation_id: str,
    direction: str,
    content: str,
    at: datetime,
    *,
    status: str = "ok",
    speaker: str | None = None,
) -> int:
    meta = {"speaker": speaker} if speaker else {}
    row_id = await repo.add_transcript(conversation_id, direction, content, status, meta)
    async with repo._pool.connection() as conn:
        await conn.execute("UPDATE transcript SET created_at = %s WHERE id = %s", (at, row_id))
    return row_id


async def _seed(repo: Repo) -> None:
    await _add(repo, MINE, "in", "Which paints did we pick for the kitchen?", T0, speaker="Alex")
    await _add(repo, MINE, "out", "You picked a pale green paint.", T0 + timedelta(minutes=1))
    await _add(repo, OTHER, "in", "kitchen paint kitchen paint kitchen", T0 + timedelta(hours=1))
    await _add(repo, OTHER, "out", "Secret of the other chat: kitchen.", T0 + timedelta(hours=1))
    await _add(
        repo, MINE, "out", "[error] kitchen boom", T0 + timedelta(minutes=2), status="failed"
    )
    await _add(repo, MINE, "task", "kitchen reminder prompt", T0 + timedelta(minutes=3))
    await _add(repo, MINE, "inject", "kitchen sensor event", T0 + timedelta(minutes=4))
    await _add(repo, MINE, "out", " [IGNORE]\n", T0 + timedelta(minutes=5))


async def test_search_uses_stems_and_stays_in_scope(repo: Repo) -> None:
    await _seed(repo)
    hits = await repo.search_transcript([MINE], "paint", limit=50)
    assert [h.direction for h in hits] == ["out", "in"]  # newest first
    assert all(h.conversation_id == MINE for h in hits)
    assert hits[1].speaker == "Alex"
    assert "paint" in hits[0].snippet.lower()


async def test_search_never_returns_another_conversation(repo: Repo) -> None:
    await _seed(repo)
    hits = await repo.search_transcript([MINE], "kitchen", limit=50)
    assert [h.conversation_id for h in hits] == [MINE]
    assert all("Secret" not in h.snippet for h in hits)
    assert await repo.search_transcript([], "kitchen", limit=50) == []


async def test_search_skips_failed_task_inject_and_silent_rows(repo: Repo) -> None:
    await _seed(repo)
    for words in ("boom", "reminder", "sensor", "IGNORE"):
        assert await repo.search_transcript([MINE], words, limit=50) == []


async def test_search_syntax_and_limit(repo: Repo) -> None:
    await _seed(repo)
    assert len(await repo.search_transcript([MINE], "paint", limit=1)) == 1
    assert await repo.search_transcript([MINE], '"pale green" -kitchen', limit=5)
    assert await repo.search_transcript([MINE], "the", limit=5) == []  # stop word only
    assert await repo.search_transcript([MINE], "   ", limit=5) == []


async def test_read_window_stays_in_scope(repo: Repo) -> None:
    await _seed(repo)
    page = await repo.read_transcript(
        [MINE], T0 - timedelta(days=1), T0 + timedelta(days=1), max_rows=50, max_chars=10_000
    )
    assert [r.content for r in page.rows] == [
        "Which paints did we pick for the kitchen?",
        "You picked a pale green paint.",
    ]
    assert page.cursor is None


async def test_read_window_bounds(repo: Repo) -> None:
    await _seed(repo)
    page = await repo.read_transcript(
        [MINE], T0 + timedelta(minutes=1), T0 + timedelta(hours=2), max_rows=50, max_chars=10_000
    )
    assert [r.direction for r in page.rows] == ["out"]
    page = await repo.read_transcript(
        [MINE], T0, T0 + timedelta(minutes=1), max_rows=50, max_chars=10_000
    )
    assert [r.direction for r in page.rows] == ["in"]  # the end is not included


async def test_read_pages_with_a_cursor(repo: Repo) -> None:
    for i in range(7):
        await _add(repo, MINE, "in", f"m{i}", T0 + timedelta(seconds=i))
    start, end = T0, T0 + timedelta(hours=1)
    seen: list[str] = []
    cursor = None
    while True:
        page = await repo.read_transcript(
            [MINE], start, end, cursor=cursor, max_rows=3, max_chars=10_000
        )
        assert len(page.rows) <= 3
        seen += [r.content for r in page.rows]
        if page.cursor is None:
            break
        cursor = page.cursor
    assert seen == [f"m{i}" for i in range(7)]


async def test_a_cursor_of_another_conversation_gives_an_empty_page(repo: Repo) -> None:
    await _seed(repo)
    other_id = await _add(repo, OTHER, "in", "earlier other", T0 - timedelta(hours=1))
    page = await repo.read_transcript(
        [MINE],
        T0 - timedelta(days=1),
        T0 + timedelta(days=1),
        cursor=str(other_id),
        max_rows=50,
        max_chars=10_000,
    )
    assert page.rows == []
    with pytest.raises(ValueError):
        await repo.read_transcript(
            [MINE], T0, T0 + timedelta(days=1), cursor="1; DROP", max_rows=5, max_chars=100
        )


async def test_the_text_search_index_exists(repo: Repo) -> None:
    async with repo._pool.connection() as conn:
        cur = await conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_transcript_fts'"
        )
        row = await cur.fetchone()
    assert row is not None
    assert "gin" in row["indexdef"].lower()
    assert "to_tsvector('english'" in row["indexdef"]
