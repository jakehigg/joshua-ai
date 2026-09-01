"""The indexer — one diff-and-repair path for every source.

For each configured source: ``list_documents()``, diff ``(person_id, path,
rev)`` against the ``kb_chunk`` index, re-chunk + re-embed only changed
documents, and delete rows for documents that vanished. Every source has a
single-flight lock, so its passes never overlap. Per-document errors are logged
and skipped; a source that fails to list records its error and never blocks
another source.

A document can fail for a fault of its own, such as a person id that names no
row. Its revision is never stored, so every following pass sees it as changed
and fails it again. Such a document is held as **unindexable** at that
revision: it is passed over until its content changes, a full reindex asks for
it, or the process restarts. The condition is one entry in the status, not a
count that climbs forever.

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
from joshua_core.memory.skills import SKILL_KIND, is_skill_path, parse_skill, skill_slug
from joshua_core.memory.sources import Document, Source
from joshua_core.memory.store import MemoryStore

logger = get_logger("memory.indexer")


def _kind_from_uri(uri: str) -> str:
    """The document kind is the leading path segment (``blog`` | ``wiki`` |
    ``shared`` for the files source; the adapter name for a flat source)."""
    return uri.split("/", 1)[0]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# SQLSTATE class 23 is an integrity constraint violation: the row breaks a rule
# of the schema, so the same row breaks it again on every pass.
_CONSTRAINT_CLASS = "23"


def _is_unindexable(exc: BaseException) -> bool:
    """True when the document is at fault, and a retry gives the same answer.

    A constraint violation and a malformed document are faults of the document.
    Everything else, such as a lost connection or a lost embedding model, is a
    fault of the run, and the next pass is allowed to try again.
    """
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate.startswith(_CONSTRAINT_CLASS):
        return True
    return isinstance(exc, ValueError | TypeError)


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
        # source -> {(person_id, uri): the revision that could not be stored}.
        # Held in the process: a restart is a reasonable time to try once more.
        self._unindexable: dict[str, dict[tuple[str | None, str], str]] = {
            name: {} for name in sources
        }

    @property
    def sources(self) -> dict[str, Source]:
        return self._sources

    def status(self) -> dict[str, Any]:
        """Per-source last run, result counts, last error, and what is unindexable.

        ``unindexable`` names the documents that are held back and why they are
        held back. It is a condition an operator acts on, so it names the paths
        rather than counting the same failure again on every pass.
        """
        out: dict[str, Any] = {}
        for name, state in self._status.items():
            held = self._unindexable[name]
            out[name] = {
                **state,
                "unindexable": {
                    "count": len(held),
                    "paths": sorted(uri for _, uri in held)[:20],
                },
            }
        return out

    async def _index_document(self, doc: Document) -> int:
        if doc.source == "files" and is_skill_path(doc.uri):
            return await self._index_skill(doc)
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

    async def _index_skill(self, doc: Document) -> int:
        """Index a taught skill: one ``kind='skill'`` row per trigger phrase.

        The trigger phrase alone is embedded (no title breadcrumb — the match is
        phrase against phrase); the row text is the full instructions. A file
        that does not parse is skipped with one warning, and its rows are cleared
        so a broken edit stops firing. An empty trigger list writes no rows.
        """
        skill = parse_skill(doc.frontmatter, doc.text, path=doc.uri)
        if skill is None:
            logger.warning({"message": "taught skill unparseable; skipped", "path": doc.uri})
            await self._store.kb_delete_item(doc.source, doc.person_id, doc.uri)
            return 0
        embedded: list[tuple[str, str, list[float]]] = []
        for trigger in skill.triggers:
            vec = await asyncio.to_thread(embed_module.embed, trigger, self._embed_model)
            if vec is None:
                raise RuntimeError("embedding unavailable (fastembed not loaded?)")
            embedded.append((trigger, skill.instructions, vec))
        return await self._store.kb_replace_item(
            source=doc.source,
            person_id=doc.person_id,
            path=doc.uri,
            kind=SKILL_KIND,
            title=skill.name or skill_slug(doc.uri),
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

                held = self._unindexable[name]
                # A document that vanished, or that came back at a new revision,
                # is no longer the document that failed.
                for key in [k for k in held if k not in live_keys]:
                    del held[key]
                if full:
                    # An operator asked for the whole source. Try them again.
                    held.clear()
                skipped = [d for d in changed if held.get((d.person_id, d.uri)) == d.rev]
                changed = [d for d in changed if held.get((d.person_id, d.uri)) != d.rev]

                indexed = failed = 0
                # A lost embedding model fails every document of every pass. One
                # line for each document buries the cause, so the pass is counted
                # and reported one time. A single bad document keeps its own line,
                # because that is a fault of that document.
                embed_ok = embed_module.is_available()
                first_error = ""
                first_path = ""
                for doc in changed:
                    try:
                        await self._index_document(doc)
                        indexed += 1
                        held.pop((doc.person_id, doc.uri), None)
                    except Exception as e:  # noqa: BLE001 — one bad doc must not sink the pass
                        failed += 1
                        if not embed_ok:
                            first_error = first_error or str(e)
                            first_path = first_path or doc.uri
                            continue
                        unindexable = _is_unindexable(e)
                        if unindexable:
                            held[(doc.person_id, doc.uri)] = doc.rev
                        logger.warning(
                            {
                                "message": (
                                    "kb document unindexable" if unindexable else "kb index failed"
                                ),
                                "source": name,
                                "path": doc.uri,
                                "person": doc.person_id,
                                "error": str(e),
                                # A held document is not tried again, so this
                                # line is written one time and not each pass.
                                "held": unindexable,
                            }
                        )
                if not embed_ok and failed:
                    logger.warning(
                        {
                            "message": "kb index failed for the pass",
                            "source": name,
                            "failed": failed,
                            "example_path": first_path,
                            "error": first_error,
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
                    "skipped": len(skipped),
                    "unindexable": len(held),
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
