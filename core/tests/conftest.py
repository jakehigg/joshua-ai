"""Fixtures for the DB-backed integration tests, and ``no_http``.

Each fixture connects to ``DATABASE_URL`` (a pgvector service in CI), applies the
schema, and truncates every table so tests do not see each other's rows. These
fixtures are only used by tests marked ``integration``, which run against a live
Postgres.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest
from joshua_core.store.db import Database
from joshua_core.store.repo import Repo

_TABLES = (
    "channels, people, person_handles, conversations, tasks, transcript, kb_event, kb_chunk, "
    "usage_event, snapshot_result, limits_snapshot"
)


@pytest.fixture
async def db():
    database = Database(os.environ["DATABASE_URL"])
    await database.connect()
    await database.init_schema()
    async with database.pool.connection() as conn:
        await conn.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE")
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def repo(db) -> Repo:
    return Repo(db)


@pytest.fixture
def no_http(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail the test on any httpx request, and record each one."""
    calls: list[str] = []

    async def refuse_async(self: Any, request: httpx.Request, **_: Any) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError(f"an HTTP request for the limits: {request.url}")

    def refuse(self: Any, request: httpx.Request, **_: Any) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError(f"an HTTP request for the limits: {request.url}")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse_async)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    return calls
