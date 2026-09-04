"""Async PostgreSQL pool and the self-creating kernel schema.

One SQL constant, applied at startup with ``CREATE TABLE IF NOT EXISTS`` and
``ADD COLUMN IF NOT EXISTS`` — additive only, no migration tool. Single writer,
so this stays simple.
"""

from __future__ import annotations

from joshua_shared.log import get_logger
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = get_logger("store.db")


SCHEMA_SQL = """
-- pgvector, for the kb_chunk embedding column below.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS channels (
    id                TEXT PRIMARY KEY,
    channel_type      TEXT NOT NULL,
    display_name      TEXT,
    default_person_id TEXT,
    config            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- session_mode: 'per_person' (DMs — one thread per person) or 'shared' (group
-- chats — one thread for the whole channel, turns labelled by speaker).
ALTER TABLE channels ADD COLUMN IF NOT EXISTS session_mode TEXT NOT NULL DEFAULT 'per_person';

CREATE TABLE IF NOT EXISTS people (
    id            TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS person_handles (
    channel_type TEXT NOT NULL,
    handle       TEXT NOT NULL,
    person_id    TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY (channel_type, handle)
);

CREATE TABLE IF NOT EXISTS conversations (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    channel_id     TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    person_id      TEXT REFERENCES people(id) ON DELETE SET NULL,
    sdk_session_id TEXT,
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One conversation per (channel, person). person_id NULL collapses to a single
-- "unidentified" conversation per channel via the partial unique indexes below.
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_channel_person
    ON conversations (channel_id, person_id)
    WHERE person_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_channel_anon
    ON conversations (channel_id)
    WHERE person_id IS NULL;

-- Last physical channel a turn ran on. NULL falls back to channel_id.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS last_channel_id TEXT;
-- Date the current SDK session started; the phase 4 daily rollover reads it.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS session_started_on DATE;

CREATE TABLE IF NOT EXISTS tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    prompt          TEXT NOT NULL,
    cron_expr       TEXT,
    next_run_at     TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    last_run_at     TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks (next_run_at) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS transcript (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id UUID NOT NULL,
    direction       TEXT NOT NULL,
    content         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'ok',
    meta            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_transcript_conv ON transcript (conversation_id, created_at);
-- Recent-window scans over the transcript.
CREATE INDEX IF NOT EXISTS idx_transcript_created ON transcript (created_at DESC);

-- KB retrieval audit (phase 4). One row per injection decision or kb tool call;
-- `results` carries what the model was given. Write-only from the app.
CREATE TABLE IF NOT EXISTS kb_event (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id UUID,
    turn_id         TEXT,
    kind            TEXT NOT NULL,               -- 'inject' | 'search' | 'read'
    query           TEXT NOT NULL DEFAULT '',
    decision        TEXT,                        -- inject only: 'full'|'hint'|'none'
    prefer_recent   BOOLEAN,
    best_sim        REAL,
    duration_ms     INT,
    results         JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kb_event_created ON kb_event (created_at DESC);
-- Join kb_event.turn_id to the outgoing transcript row's meta for per-turn timings.
CREATE INDEX IF NOT EXISTS idx_transcript_turn
    ON transcript ((meta->>'turn_id')) WHERE direction = 'out';

-- In-core RAG index (phase 4). One row per chunk of a markdown document, scoped
-- by person; person_id NULL is a shared document. source is the
-- adapter name ('files' | 'memos' | …); path is the adapter's document uri.
CREATE TABLE IF NOT EXISTS kb_chunk (
    id           BIGSERIAL PRIMARY KEY,
    person_id    TEXT REFERENCES people(id) ON DELETE CASCADE,   -- NULL = shared/
    source       TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT '',   -- files → 'journal' | 'profile' | 'wiki' | 'shared'
    path         TEXT NOT NULL,
    provenance   TEXT NOT NULL DEFAULT 'own',   -- 'own' | 'external'
    title        TEXT NOT NULL DEFAULT '',
    heading      TEXT NOT NULL DEFAULT '',
    chunk_index  INT NOT NULL,
    text         TEXT NOT NULL,
    embedding    vector(384),
    file_mtime   TIMESTAMPTZ NOT NULL,
    file_sha256  TEXT NOT NULL,
    doc_date     DATE,                       -- journal pages: the day the page belongs to
    indexed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- A NULL person_id does not dedupe under a plain UNIQUE, so key on COALESCE.
CREATE UNIQUE INDEX IF NOT EXISTS uq_kb_chunk_doc
    ON kb_chunk (COALESCE(person_id, ''), source, path, chunk_index);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_embedding
    ON kb_chunk USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_scope ON kb_chunk (person_id, source);
"""


class Database:
    """Owns the async connection pool and exposes it to the repo."""

    def __init__(self, database_url: str, *, min_size: int = 1, max_size: int = 10):
        self._url = database_url
        self._min_size = min_size
        self._max_size = max_size
        self.pool: AsyncConnectionPool | None = None

    async def connect(self) -> None:
        # open=False + explicit open() avoids the "opened in constructor" warning
        # and lets the lifespan control startup ordering.
        self.pool = AsyncConnectionPool(
            self._url,
            min_size=self._min_size,
            max_size=self._max_size,
            open=False,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
        await self.pool.open(wait=True, timeout=30)
        logger.info({"message": "Postgres pool open"})

    async def init_schema(self) -> None:
        assert self.pool is not None, "connect() first"
        async with self.pool.connection() as conn:
            await conn.execute(SCHEMA_SQL)
        logger.info({"message": "Schema verified"})

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
            logger.info({"message": "Postgres pool closed"})
