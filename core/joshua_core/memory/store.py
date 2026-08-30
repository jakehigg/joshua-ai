"""Data access for the ``kb_chunk`` index — all memory SQL lives here.

Ported from joshua-kb's ``app/repo.py`` (``kb_replace_item``, ``kb_search``,
``kb_index_state``, ``kb_delete_item``) with the person-scope predicate: a
person sees their own rows plus shared rows (``person_id IS NULL``); a
group turn (``person_id`` None) sees shared rows only.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from joshua_shared.log import get_logger
from psycopg_pool import AsyncConnectionPool

from joshua_core.memory.models import KbChunk

logger = get_logger("memory.store")


def _vec_literal(embedding: list[float]) -> str:
    """Render an embedding as a pgvector text literal for ``%s::vector`` params."""
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


def _kb_chunk(row: dict[str, Any]) -> KbChunk:
    return KbChunk(
        id=row["id"],
        person_id=row.get("person_id"),
        source=row["source"],
        kind=row.get("kind") or "",
        path=row["path"],
        provenance=row.get("provenance") or "own",
        title=row.get("title") or "",
        heading=row.get("heading") or "",
        chunk_index=row.get("chunk_index") or 0,
        text=row["text"],
        similarity=row.get("similarity"),
        file_mtime=row.get("file_mtime"),
        file_sha256=row.get("file_sha256") or "",
        doc_date=row.get("doc_date"),
        indexed_at=row.get("indexed_at"),
    )


class MemoryStore:
    """The ``kb_chunk`` table, wrapped over the shared connection pool."""

    def __init__(self, pool: AsyncConnectionPool):
        self._pool = pool

    async def kb_index_state(self, source: str) -> dict[tuple[str | None, str], str]:
        """``{(person_id, path): file_sha256}`` for every indexed document of
        ``source`` — the indexer diffs this against the adapter's live listing."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT person_id, path, MIN(file_sha256) AS sha FROM kb_chunk "
                "WHERE source = %s GROUP BY person_id, path",
                (source,),
            )
            rows = await cur.fetchall()
        return {(r["person_id"], r["path"]): r["sha"] for r in rows}

    async def kb_replace_item(
        self,
        *,
        source: str,
        person_id: str | None,
        path: str,
        kind: str,
        title: str,
        provenance: str,
        file_sha256: str,
        file_mtime: datetime,
        doc_date: date | None,
        chunks: Sequence[tuple[str, str, list[float]]],  # (heading, text, embedding)
    ) -> int:
        """Swap in the new chunks for one document atomically, so a concurrent
        search never sees a half-updated document."""
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM kb_chunk WHERE source = %s "
                    "AND person_id IS NOT DISTINCT FROM %s AND path = %s",
                    (source, person_id, path),
                )
                for i, (heading, text, embedding) in enumerate(chunks):
                    await conn.execute(
                        """
                        INSERT INTO kb_chunk
                            (person_id, source, kind, path, provenance, title, heading,
                             chunk_index, text, embedding, file_mtime, file_sha256, doc_date)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s)
                        """,
                        (
                            person_id,
                            source,
                            kind,
                            path,
                            provenance,
                            title,
                            heading,
                            i,
                            text,
                            _vec_literal(embedding),
                            file_mtime,
                            file_sha256,
                            doc_date,
                        ),
                    )
        return len(chunks)

    async def kb_delete_item(self, source: str, person_id: str | None, path: str) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM kb_chunk WHERE source = %s "
                "AND person_id IS NOT DISTINCT FROM %s AND path = %s",
                (source, person_id, path),
            )
            return cur.rowcount

    async def kb_purge_sources(self, keep: Sequence[str]) -> int:
        """Delete rows whose ``source`` is no longer configured. ``keep`` is the
        set of currently configured adapter names."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM kb_chunk WHERE NOT (source = ANY(%s))", (list(keep),)
            )
            return cur.rowcount

    async def kb_search(
        self,
        person_id: str | None,
        embedding: list[float],
        *,
        k: int,
        min_sim: float,
        sources: Sequence[str] | None = None,
    ) -> list[KbChunk]:
        """Top-k chunks above the cosine floor, scoped to this person plus shared.

        ``person_id = %s`` admits the caller's own rows; ``person_id IS NULL``
        admits shared rows. A group turn (``person_id`` None) matches shared only,
        because ``person_id = NULL`` is never true.
        """
        vec = _vec_literal(embedding)
        source_clause = "AND source = ANY(%s)" if sources else ""
        params: list[Any] = [vec, person_id]
        if sources:
            params.append(list(sources))
        params += [vec, min_sim, vec, k]
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"""
                SELECT *, 1 - (embedding <=> %s::vector) AS similarity
                FROM kb_chunk
                WHERE embedding IS NOT NULL
                  AND (person_id = %s OR person_id IS NULL)
                  {source_clause}
                  AND 1 - (embedding <=> %s::vector) >= %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                params,
            )
            rows = await cur.fetchall()
        return [_kb_chunk(r) for r in rows]

    async def kb_stats(self) -> list[dict[str, Any]]:
        """Per-source document/chunk counts for ``/admin/kb/status``."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT source, COUNT(DISTINCT (person_id, path)) AS items, COUNT(*) AS chunks, "
                "MAX(indexed_at) AS last_indexed FROM kb_chunk GROUP BY source ORDER BY source"
            )
            rows = await cur.fetchall()
        return [
            {
                "source": r["source"],
                "items": r["items"],
                "chunks": r["chunks"],
                "last_indexed": r["last_indexed"].isoformat() if r["last_indexed"] else None,
            }
            for r in rows
        ]
