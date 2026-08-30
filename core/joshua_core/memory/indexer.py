"""The indexer — one diff-and-repair path for every source.

For each configured source: ``list_documents()``, diff ``(person_id, path,
rev)`` against the ``kb_chunk`` index, re-chunk + re-embed only changed
documents, and delete rows for documents that vanished. Every source has a
single-flight lock, so its passes never overlap. Per-document errors are logged
and skipped; a source that fails to list records its error and never blocks
another source.

The startup pass makes every deploy self-heal (a first-boot empty index is just
the degenerate diff where everything is new). The interval loop keeps the
``files`` source fresh; other adapters run on their own schedule.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from joshua_shared.log import get_logger

from joshua_core.memory import embed as embed_module
from joshua_core.memory.chunker import chunk_markdown
from joshua_core.memory.sources import Document, Source
from joshua_core.memory.store import MemoryStore

logger = get_logger("memory.indexer")


def _kind_from_uri(uri: str) -> str:
    """The document kind is the leading path segment (``blog`` | ``wiki`` |
    ``shared`` for the files source; the adapter name for a flat source)."""
    return uri.split("/", 1)[0]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Indexer:
    def __init__(
        self,
        store: MemoryStore,
        sources: dict[str, Source],
        *,
        embed_model: str,
        chunk_chars: int,
    ):
        self._store = store
        self._sources = sources
        self._embed_model = embed_model
        self._chunk_chars = chunk_chars
        self._locks = {name: asyncio.Lock() for name in sources}
        self._status: dict[str, dict[str, Any]] = {
            name: {"last_run": None, "last_result": None, "last_error": None, "running": False}
            for name in sources
        }

    @property
    def sources(self) -> dict[str, Source]:
        return self._sources

    def status(self) -> dict[str, Any]:
        """Per-source last run, result counts, and last error for the admin API."""
        return {name: dict(state) for name, state in self._status.items()}

    async def _index_document(self, doc: Document) -> int:
        chunks = chunk_markdown(doc.text, self._chunk_chars)
        embedded: list[tuple[str, str, list[float]]] = []
        for c in chunks:
            vec = await asyncio.to_thread(
                embed_module.embed, c.embed_text(doc.title), self._embed_model
            )
            if vec is None:
                raise RuntimeError("embedding unavailable (fastembed not loaded?)")
            embedded.append((c.heading, c.text, vec))
        return await self._store.kb_replace_item(
            source=doc.source,
            person_id=doc.person_id,
            path=doc.uri,
            kind=_kind_from_uri(doc.uri),
            title=doc.title,
            provenance=doc.provenance,
            file_sha256=doc.rev,
            file_mtime=doc.updated_at,
            doc_date=doc.doc_date,
            chunks=embedded,
        )

    async def reindex(
        self, *, source: str | None = None, person: str | None = None, full: bool = False
    ) -> dict[str, Any]:
        """Reconcile one source (or all). Purges rows of any source no longer
        configured, then diffs each named source. Returns per-source stats."""
        try:
            purged = await self._store.kb_purge_sources(list(self._sources))
            if purged:
                logger.info({"message": "kb purged unconfigured sources", "chunks": purged})
        except Exception as e:  # noqa: BLE001 — hygiene must not sink the pass
            logger.warning({"message": "kb source purge failed", "error": str(e)})

        names = [source] if source else list(self._sources)
        results: dict[str, Any] = {}
        for name in names:
            if name not in self._sources:
                results[name] = {"error": "unknown source"}
                continue
            try:
                results[name] = await self._reconcile_source(name, person=person, full=full)
            except Exception as e:  # noqa: BLE001 — one source must not sink the rest
                msg = str(e) or repr(e)
                results[name] = {"error": msg}
                self._status[name]["last_error"] = msg
                logger.error(
                    {"message": "kb source reconcile failed", "source": name, "error": msg}
                )
        return results

    async def _reconcile_source(
        self, name: str, *, person: str | None, full: bool
    ) -> dict[str, int]:
        source = self._sources[name]
        async with self._locks[name]:
            self._status[name]["running"] = True
            try:
                docs = [doc async for doc in source.list_documents()]
                if person is not None:
                    docs = [d for d in docs if d.person_id == person]
                state = await self._store.kb_index_state(name)
                live_keys = {(d.person_id, d.uri) for d in docs}
                changed = [d for d in docs if full or state.get((d.person_id, d.uri)) != d.rev]
                removed = [
                    key
                    for key in state
                    if key not in live_keys and (person is None or key[0] == person)
                ]

                indexed = failed = 0
                for doc in changed:
                    try:
                        await self._index_document(doc)
                        indexed += 1
                    except Exception as e:  # noqa: BLE001 — one bad doc must not sink the pass
                        failed += 1
                        logger.warning(
                            {
                                "message": "kb index failed",
                                "source": name,
                                "path": doc.uri,
                                "person": doc.person_id,
                                "error": str(e),
                            }
                        )
                for person_id, path in removed:
                    await self._store.kb_delete_item(name, person_id, path)

                stats = {
                    "listed": len(docs),
                    "changed": len(changed),
                    "indexed": indexed,
                    "removed": len(removed),
                    "failed": failed,
                }
                self._status[name].update(
                    last_run=_now_iso(),
                    last_result=stats,
                    last_error=None if not failed else f"{failed} document(s) failed",
                )
                logger.info({"message": "kb reconciled", "source": name, "full": full, **stats})
                return stats
            finally:
                self._status[name]["running"] = False


class IndexerLoop:
    """Background driver: a startup full reconcile, then per-source intervals."""

    def __init__(self, indexer: Indexer, intervals: dict[str, float]):
        self._indexer = indexer
        self._intervals = intervals
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="memory-indexer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        try:
            await self._indexer.reindex()
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            logger.error({"message": "kb startup reconcile failed", "error": str(e)})

        # Monotonic seconds until each source is next due, decremented by the
        # tick length; the tick is the shortest configured interval.
        due = dict(self._intervals)
        tick = min(self._intervals.values()) if self._intervals else 60.0
        while True:
            try:
                await asyncio.sleep(tick)
                for name in list(self._intervals):
                    due[name] -= tick
                    if due[name] <= 0:
                        due[name] = self._intervals[name]
                        await self._indexer.reindex(source=name)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — never let the loop die
                logger.error({"message": "kb indexer loop error", "error": str(e)})
                await asyncio.sleep(min(tick, 60.0))
