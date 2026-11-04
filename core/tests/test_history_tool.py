"""Offline tests for the ``history`` builtin: the scope, the limits, the pages.

``FakeTranscriptRepo`` is the in-memory equivalent of ``Repo.search_transcript``
and ``Repo.read_transcript``. It uses the same visibility rule and page rule
(``store.transcript``), and it records the conversation ids of each call. The
SQL of the same rules is in ``test_history_integration.py``.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from joshua_core.delivery import IGNORE_MARKER
from joshua_core.engine.tools import ToolDeps, history
from joshua_core.store.models import Channel, Conversation
from joshua_core.store.transcript import (
    SILENT_REPLY,
    TranscriptHit,
    TranscriptPage,
    TranscriptRow,
    decode_cursor,
    is_visible,
    page_rows,
)

T0 = datetime(2026, 3, 10, 14, 0, tzinfo=UTC)
TZ = "America/New_York"
MINE = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class FakeTranscriptRepo:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.search_ids: list[list[str]] = []
        self.read_ids: list[list[str]] = []

    def add(
        self,
        conversation_id: str,
        direction: str,
        content: str,
        *,
        at: datetime,
        status: str = "ok",
        speaker: str | None = None,
    ) -> None:
        self.rows.append(
            {
                "id": len(self.rows) + 1,
                "conversation_id": conversation_id,
                "direction": direction,
                "content": content,
                "status": status,
                "speaker": speaker,
                "created_at": at,
            }
        )

    def _visible(self, ids: Collection[str]) -> list[dict[str, Any]]:
        return [
            r
            for r in self.rows
            if r["conversation_id"] in ids and is_visible(r["direction"], r["status"], r["content"])
        ]

    async def search_transcript(
        self, conversation_ids: Collection[str], query: str, *, limit: int
    ) -> list[TranscriptHit]:
        self.search_ids.append(list(conversation_ids))
        words = [w.lower() for w in query.split()]
        found = [
            r
            for r in self._visible(conversation_ids)
            if all(w in r["content"].lower() for w in words)
        ]
        found.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return [
            TranscriptHit(
                id=r["id"],
                conversation_id=r["conversation_id"],
                direction=r["direction"],
                speaker=r["speaker"],
                created_at=r["created_at"],
                snippet=r["content"],
            )
            for r in found[:limit]
        ]

    async def read_transcript(
        self,
        conversation_ids: Collection[str],
        start: datetime,
        end: datetime,
        *,
        cursor: str | None = None,
        max_rows: int,
        max_chars: int,
    ) -> TranscriptPage:
        self.read_ids.append(list(conversation_ids))
        after = decode_cursor(cursor)
        rows = sorted(
            (r for r in self._visible(conversation_ids) if start <= r["created_at"] < end),
            key=lambda r: (r["created_at"], r["id"]),
        )
        if after is not None:
            mark = next((r for r in self._visible(conversation_ids) if r["id"] == after), None)
            if mark is None:
                return TranscriptPage(rows=[], cursor=None)
            key = (mark["created_at"], mark["id"])
            rows = [r for r in rows if (r["created_at"], r["id"]) > key]
        found = [
            TranscriptRow(
                id=r["id"],
                conversation_id=r["conversation_id"],
                direction=r["direction"],
                speaker=r["speaker"],
                created_at=r["created_at"],
                content=r["content"],
            )
            for r in rows[: max_rows + 1]
        ]
        return page_rows(found, max_rows=max_rows, max_chars=max_chars)


def _deps(repo: FakeTranscriptRepo, conversation_id: str = MINE) -> ToolDeps:
    return ToolDeps(
        repo=repo,  # type: ignore[arg-type]
        conversation=Conversation(id=conversation_id, channel_id="telegram:1", person_id="alex"),
        channel=Channel(id="telegram:1", channel_type="telegram"),
        tz=TZ,
        person_id="alex",
    )


def _seed() -> FakeTranscriptRepo:
    repo = FakeTranscriptRepo()
    repo.add(MINE, "in", "What paint did we pick for the kitchen?", at=T0)
    repo.add(MINE, "out", "You picked a pale green paint.", at=T0 + timedelta(minutes=1))
    repo.add(OTHER, "in", "kitchen paint kitchen paint kitchen paint", at=T0 + timedelta(hours=1))
    repo.add(OTHER, "out", "Secret of the other chat: kitchen.", at=T0 + timedelta(hours=1))
    return repo


# ── the scope ───────────────────────────────────────────────────────────────


async def test_search_finds_a_message_of_the_current_conversation() -> None:
    repo = _seed()
    text = await history.do_search_history(_deps(repo), query="paint")
    assert "pale green" in text
    assert "[2026-03-10 10:01] you:" in text  # 14:01 UTC is 10:01 in New York
    assert "alex:" in text


async def test_search_never_returns_a_row_of_another_conversation() -> None:
    """The other conversation matches the query better, and is still absent."""
    repo = _seed()
    text = await history.do_search_history(_deps(repo), query="kitchen", limit=50)
    assert "Secret" not in text
    assert "kitchen paint kitchen" not in text
    assert repo.search_ids == [[MINE]]


async def test_read_never_returns_a_row_of_another_conversation() -> None:
    repo = _seed()
    text = await history.do_read_history(
        _deps(repo), start="2026-03-10T00:00", end="2026-03-11T00:00"
    )
    assert "pale green" in text
    assert "Secret" not in text
    assert repo.read_ids == [[MINE]]


async def test_a_session_of_the_other_conversation_sees_only_its_own_rows() -> None:
    repo = _seed()
    text = await history.do_search_history(_deps(repo, OTHER), query="paint")
    assert "pale green" not in text
    assert repo.search_ids == [[OTHER]]


async def test_a_cursor_from_another_conversation_gives_nothing() -> None:
    repo = _seed()
    other_row = next(r["id"] for r in repo.rows if r["conversation_id"] == OTHER)
    text = await history.do_read_history(
        _deps(repo),
        start="2026-03-01",
        end="2026-03-20",
        cursor=str(other_row),
    )
    assert "Secret" not in text
    assert "No message" in text


def _tools(monkeypatch: pytest.MonkeyPatch, deps: ToolDeps) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_server(*, name: str, version: str, tools: list[Any]) -> dict[str, Any]:
        captured.update({t.name: t for t in tools})
        return {"type": "sdk", "name": name}

    monkeypatch.setattr(history, "create_sdk_mcp_server", fake_server)
    server = history.build_history_server(deps)
    assert server["name"] == "history"
    return captured


def test_no_tool_takes_a_conversation_channel_or_person(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = _tools(monkeypatch, _deps(FakeTranscriptRepo()))
    assert set(tools) == {"search_history", "read_history"}
    assert set(tools["search_history"].input_schema["properties"]) == {"query", "limit"}
    assert set(tools["read_history"].input_schema["properties"]) == {"start", "end", "cursor"}
    for t in tools.values():
        assert "this conversation only" in t.description


async def test_a_scope_argument_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model that adds an id of another conversation still reads its own."""
    repo = _seed()
    tools = _tools(monkeypatch, _deps(repo))
    sneaky = {"conversation_id": OTHER, "channel": "telegram:2", "person": "mia"}
    found = await tools["search_history"].handler({"query": "kitchen", **sneaky})
    read = await tools["read_history"].handler(
        {"start": "2026-03-10", "end": "2026-03-11", **sneaky}
    )
    for reply in (found, read):
        text = reply["content"][0]["text"]
        assert "Secret" not in text
    assert repo.search_ids == [[MINE]]
    assert repo.read_ids == [[MINE]]


# ── which rows a reader sees ────────────────────────────────────────────────


def test_only_delivered_messages_are_visible() -> None:
    assert is_visible("in", "ok", "hello")
    assert is_visible("out", "ok", "hello")
    assert not is_visible("out", "failed", "[error] boom")
    assert not is_visible("task", "ok", "remind alex")
    assert not is_visible("inject", "ok", "the door opened")
    assert not is_visible("out", "ok", "  [IGNORE] \n")
    assert not is_visible("out", "ok", "  ")
    assert SILENT_REPLY == IGNORE_MARKER


async def test_failed_task_and_silent_rows_are_not_returned() -> None:
    repo = FakeTranscriptRepo()
    repo.add(MINE, "out", "[error] kitchen boom", at=T0, status="failed")
    repo.add(MINE, "task", "kitchen reminder prompt", at=T0)
    repo.add(MINE, "inject", "kitchen sensor event", at=T0)
    repo.add(MINE, "out", IGNORE_MARKER, at=T0)
    text = await history.do_search_history(_deps(repo), query="kitchen")
    assert text == "No message in this conversation matches."


# ── the limits and the pages ────────────────────────────────────────────────


async def test_search_limit_is_bounded() -> None:
    repo = FakeTranscriptRepo()
    for i in range(80):
        repo.add(MINE, "in", f"note {i}", at=T0 + timedelta(minutes=i))
    text = await history.do_search_history(_deps(repo), query="note", limit=500)
    assert text.count("\n[") == history.SEARCH_MAX_LIMIT
    text = await history.do_search_history(_deps(repo), query="note")
    assert text.count("\n[") == history.SEARCH_DEFAULT_LIMIT
    assert text.splitlines()[1].endswith("note 79")  # newest first
    text = await history.do_search_history(_deps(repo), query="note", limit="x")
    assert text.count("\n[") == history.SEARCH_DEFAULT_LIMIT


async def test_a_long_snippet_is_cut() -> None:
    repo = FakeTranscriptRepo()
    repo.add(MINE, "in", "word " * 200, at=T0)
    text = await history.do_search_history(_deps(repo), query="word")
    line = text.splitlines()[1]
    assert line.endswith("...")
    assert len(line) < history.SNIPPET_CHARS + 40


async def test_an_empty_query_searches_nothing() -> None:
    repo = _seed()
    assert "Give words" in await history.do_search_history(_deps(repo), query="  ")
    assert repo.search_ids == []


async def test_read_pages_by_rows_and_says_when_more_remains() -> None:
    repo = FakeTranscriptRepo()
    total = history.READ_MAX_ROWS * 2 + 5
    for i in range(total):
        repo.add(MINE, "in", f"m{i}", at=T0 + timedelta(seconds=i))
    deps = _deps(repo)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        text = await history.do_read_history(
            deps, start="2026-03-10", end="2026-03-11", cursor=cursor
        )
        pages += 1
        lines = [ln for ln in text.splitlines() if ln.startswith("[2026")]
        assert len(lines) <= history.READ_MAX_ROWS
        seen += [ln.rsplit(": ", 1)[1] for ln in lines]
        if "[End of this window.]" in text:
            break
        assert "call read_history again with cursor" in text
        cursor = text.rsplit('cursor "', 1)[1].split('"', 1)[0]
    assert pages == 3
    assert seen == [f"m{i}" for i in range(total)]


async def test_read_pages_by_characters() -> None:
    repo = FakeTranscriptRepo()
    body = "x" * 5_000
    for i in range(5):
        repo.add(MINE, "in", body, at=T0 + timedelta(seconds=i))
    text = await history.do_read_history(_deps(repo), start="2026-03-10", end="2026-03-11")
    assert len(text) < history.READ_MAX_CHARS + 500
    assert text.count(body) == 2
    assert "cursor" in text


def test_one_long_message_is_cut_so_the_page_moves_on() -> None:
    rows = [
        TranscriptRow(1, MINE, "in", None, T0, "y" * 50),
        TranscriptRow(2, MINE, "in", None, T0, "short"),
    ]
    page = page_rows(rows, max_rows=10, max_chars=20)
    assert [r.id for r in page.rows] == [1]
    assert page.rows[0].cut is True
    assert len(page.rows[0].content) == 20
    assert page.cursor == "1"
    assert page_rows(rows[1:], max_rows=10, max_chars=20).cursor is None


async def test_a_cut_message_is_marked() -> None:
    repo = FakeTranscriptRepo()
    repo.add(MINE, "out", "z" * (history.READ_MAX_CHARS + 10), at=T0)
    text = await history.do_read_history(_deps(repo), start="2026-03-10", end="2026-03-11")
    assert "[message cut]" in text


# ── times ───────────────────────────────────────────────────────────────────


def test_a_time_with_no_offset_is_local() -> None:
    at = history.parse_time("2026-03-10T10:00", TZ)
    assert at.astimezone(UTC) == datetime(2026, 3, 10, 14, 0, tzinfo=UTC)
    assert history.parse_time("2026-03-10", TZ).hour == 0
    assert history.parse_time("2026-03-10T14:00Z", TZ) == T0
    with pytest.raises(ValueError):
        history.parse_time("", TZ)
    with pytest.raises(ValueError):
        history.parse_time("next tuesday", TZ)


async def test_read_refuses_a_bad_window() -> None:
    repo = _seed()
    deps = _deps(repo)
    assert "ISO" in await history.do_read_history(deps, start="soon")
    assert "after start" in await history.do_read_history(
        deps, start="2026-03-11", end="2026-03-10"
    )
    assert "cursor is not valid" in await history.do_read_history(
        deps, start="2026-03-10", end="2026-03-11", cursor="abc"
    )
    assert repo.read_ids == [[MINE]]


async def test_read_with_no_end_reads_to_now() -> None:
    repo = _seed()
    text = await history.do_read_history(
        _deps(repo), start="2026-03-10", now=T0 + timedelta(minutes=30)
    )
    assert "pale green" in text


async def test_an_empty_window_says_so() -> None:
    text = await history.do_read_history(
        _deps(FakeTranscriptRepo()), start="2026-03-10", end="2026-03-11"
    )
    assert text == "No message in this conversation in this window."


def test_register_wires_the_builtin() -> None:
    wired: dict[str, object] = {}
    history.register(
        type("M", (), {"register_builtin": lambda self, n, f: wired.__setitem__(n, f)})()
    )
    assert wired == {"history": history.build_history_server}


def test_the_store_and_the_index_use_one_text_search_configuration() -> None:
    from joshua_core.store.db import SCHEMA_SQL
    from joshua_core.store.transcript import TS_CONFIG

    assert f"to_tsvector('{TS_CONFIG}', content)" in SCHEMA_SQL
    assert "CREATE INDEX IF NOT EXISTS idx_transcript_fts" in SCHEMA_SQL
