"""The store rules that need no database: the columns, the statuses, and the schema."""

from __future__ import annotations

import re

import pytest
from joshua_assignments.store import (
    FINISHED,
    PARKED,
    SCHEMA_SQL,
    STATUSES,
    UPDATABLE,
    PgStore,
    check_fields,
)


def test_the_statuses_are_the_nine_of_the_plan() -> None:
    assert STATUSES == (
        "queued",
        "running",
        "waiting",
        "paused_limits",
        "done",
        "failed",
        "cancelled",
        "budget_exceeded",
        "time_exceeded",
    )
    assert PARKED | FINISHED < set(STATUSES)
    assert not PARKED & FINISHED


def test_check_fields_refuses_an_unknown_column_and_status() -> None:
    check_fields({"status": "done", "summary": "x"})
    with pytest.raises(ValueError):
        check_fields({"person_id": "mia"})
    with pytest.raises(ValueError):
        check_fields({"status": "finished"})
    assert "person_id" not in UPDATABLE and "id" not in UPDATABLE


def test_the_schema_is_additive_only() -> None:
    upper = SCHEMA_SQL.upper()
    for word in ("DROP ", "ALTER TABLE ASSIGNMENTS.ASSIGNMENT DROP", "TRUNCATE", "RENAME"):
        assert word not in upper
    for statement in re.findall(r"CREATE (?:TABLE|INDEX|SCHEMA)[^\n]*", SCHEMA_SQL):
        assert "IF NOT EXISTS" in statement
    assert "CREATE SCHEMA IF NOT EXISTS assignments;" in SCHEMA_SQL


def test_the_schema_has_every_column_of_the_plan() -> None:
    for column in (
        "person_id",
        "conversation_id",
        "spec",
        "status",
        "model",
        "session_id",
        "page_path",
        "input",
        "output",
        "cache_read",
        "cache_write",
        "turns",
        "started_at",
        "finished_at",
        "working_s",
        "stopped_at_limits",
        "open_question",
        "asked_at",
        "answer",
        "summary",
        "follow_ups",
        "error",
        "limits_before",
        "ended_at",
        "reason",
    ):
        assert re.search(rf"\n\s+{column}\s", SCHEMA_SQL), column


async def test_a_store_that_is_not_open_refuses() -> None:
    store = PgStore("postgresql://unused")
    with pytest.raises(RuntimeError):
        await store.get(1)
    assert await store.ping() is False
    await store.close()
