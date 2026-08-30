"""Fixtures for the DB-backed integration tests.

Each fixture connects to ``DATABASE_URL`` (a pgvector service in CI), applies the
schema, and truncates every table so tests do not see each other's rows. These
fixtures are only used by tests marked ``integration``, which run against a live
Postgres.
"""

from __future__ import annotations

import os

import pytest
from joshua_core.store.db import Database
from joshua_core.store.repo import Repo

_TABLES = "channels, people, person_handles, conversations, tasks, transcript, kb_event, kb_chunk"


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
