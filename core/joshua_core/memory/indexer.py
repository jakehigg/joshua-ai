"""The indexer — one diff-and-repair path for every source.

For each configured source: ``list_documents()``, diff ``(person_id, path,
rev)`` against the ``kb_chunk`` index, re-chunk + re-embed only changed
documents, and delete rows for documents that vanished. Every source has a
single-flight lock, so its passes never overlap. Per-document errors are logged
and skipped; a source that fails to list records its error and never blocks
another source.

A document can leave no row in the index, and the diff reads a missing row as
a changed document. Both shapes then repeat on every pass forever:

* it fails for a fault of its own, such as a person id that names no row, and
  the failure is counted (**unindexable**);
* it succeeds and yields no chunk, because the file is empty, and nothing is
  counted at all (**empty**). This one is silent: the pass reports
  ``indexed: 1, failed: 0`` and writes nothing.

Either way the revision is held in the process, and the document is passed
over until its content changes, a full reindex asks for it, or the process
restarts.

The startup pass makes every deploy self-heal (a first-boot empty index is just
the degenerate diff where everything is new). The interval loop keeps the
``files`` source fresh; other adapters run on their own schedule.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from joshua_shared.log import get_logger

from joshua_core.memory import embed as embed_module
from joshua_core.memory.chunker import chunk_markdown
from joshua_core.memory.skill_registry import SkillRegistry
from joshua_core.memory.skills import (
    KIND_CONVENTION,
    SKILL_KIND,
    Skill,
    is_skill_path,
    parse_skill,
    skill_slug,
)
from joshua_core.memory.sources import Document, Source
from joshua_core.memory.store import MemoryStore

logger = get_logger("memory.indexer")


_JOURNAL_PREFIX = "wiki/journal/"
_PROFILE_PAGE = re.compile(r"^wiki/people/[^/]+\.md\Z")


def _kind_from_uri(uri: str) -> str:
    """The document kind, from its path.

    ``wiki/journal/**`` is ``journal``; a ``wiki/people/*.md`` page (a
    person's profile, or the shared profile) is ``profile``; anything else is
    the leading path segment (``wiki`` | ``shared`` for the files source; the
    adapter name for a flat source)."""
    if uri.startswith(_JOURNAL_PREFIX):
        return "journal"
    if _PROFILE_PAGE.match(uri):
        return "profile"
    return uri.split("/", 1)[0]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# SQLSTATE class 23 is an integrity constraint violation: the row breaks a rule
# of the schema, so the same row breaks it again on every pass.
_CONSTRAINT_CLASS = "23"


def _held_report(
    held: dict[tuple[str | None, str], tuple[str, str]], reason: str
) -> dict[str, Any]:
    """The count and the paths held back for one reason."""
    paths = sorted(uri for (_, uri), (_, held_reason) in held.items() if held_reason == reason)
    return {"count": len(paths), "paths": paths[:20]}


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
        skills: SkillRegistry | None = None,
        known_people: Sequence[str] | None = None,
    ):
        self._store = store
        self._sources = sources
        self._embed_model = embed_model
        self._chunk_chars = chunk_chars
        # The phrase matcher reads this. It is rebuilt from every skill file on
        # each pass over ``files``, changed or not, because a phrase match needs
        # the whole set and parsing a few dozen small files costs nothing.
        self._skills = skills if skills is not None else SkillRegistry()
        # The roster, so a skill whose ``for`` names nobody is reported. A
        # well-formed id that belongs to no person is a typo, and the skill
        # would otherwise never fire and never say why.
        self._known_people = tuple(known_people or ())
        self._locks = {name: asyncio.Lock() for name in sources}
        self._status: dict[str, dict[str, Any]] = {
            name: {"last_run": None, "last_result": None, "last_error": None, "running": False}
            for name in sources
        }
        # source -> {(person_id, uri): (revision, reason)} for a document that
        # left no row in the index at that revision. Held in the process: a
        # restart is a reasonable time to try once more.
        self._held: dict[str, dict[tuple[str | None, str], tuple[str, str]]] = {
            name: {} for name in sources
        }

    @property
    def sources(self) -> dict[str, Source]:
        return self._sources

    @property
    def skills(self) -> SkillRegistry:
        return self._skills

    def status(self) -> dict[str, Any]:
        """Per-source last run, result counts, last error, and what is held back.

        ``unindexable`` is a fault to act on, so it names the paths. ``empty``
        is a file with nothing to index, which is not a fault; it is counted
        and named so that a pass which reports no work is explainable.
        """
        out: dict[str, Any] = {}
        for name, state in self._status.items():
            held = self._held[name]
            out[name] = {
                **state,
                "unindexable": _held_report(held, "unindexable"),
                "empty": _held_report(held, "empty"),
            }
        return out

    def _rebuild_skill_registry(self, docs: list[Document]) -> None:
        """Parse every skill file on the volume and replace the registry.

        Every document is parsed, changed or not: the phrase matcher needs the
        whole set, and the diff that drives indexing says nothing about the
        files it decided to skip. A file that does not parse is left out, and
        the pass that indexes it logs why.
        """
        parsed: list[Skill] = []
        for doc in docs:
            if not is_skill_path(doc.uri):
                continue
            skill = parse_skill(
                doc.frontmatter, doc.text, path=doc.uri, known_people=self._known_people
            )
            if skill is not None:
                parsed.append(skill)
        self._skills.replace(parsed)

    async def _index_document(self, doc: Document) -> int:
        if doc.source == "files" and is_skill_path(doc.uri):
            rows = await self._index_skill(doc)
            if rows is not None:
                return rows
            # A convention page holds no trigger. It is reference material, so
            # it is indexed as an ordinary wiki page and a turn reaches it
            # through recall.
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

    async def _index_skill(self, doc: Document) -> int | None:
        """Index a command skill: one ``kind='skill'`` row per trigger phrase.

        Returns the row count, or None when the page is a convention and the
        caller must index it as an ordinary wiki page instead.

        The trigger phrase alone is embedded; the row text is the full
        instructions. A file that does not parse is skipped with one warning,
        and its rows are cleared so a broken edit stops firing. An empty trigger
        list writes no rows.

        A row is written for every command page, whatever its match mode. The
        phrase matcher does not read these rows, but writing them means
        ``match: semantic`` starts working the moment a person sets it, with no
        reindex.
        """
        skill = parse_skill(doc.frontmatter, doc.text, path=doc.uri)
        if skill is None:
            logger.warning({"message": "taught skill unparseable; skipped", "path": doc.uri})
            await self._store.kb_delete_item(doc.source, doc.person_id, doc.uri)
            return 0
        if skill.kind == KIND_CONVENTION:
            return None
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
                if name == "files" and person is None:
                    self._rebuild_skill_registry(docs)
                state = await self._store.kb_index_state(name)
                live_keys = {(d.person_id, d.uri) for d in docs}
                changed = [d for d in docs if full or state.get((d.person_id, d.uri)) != d.rev]
                removed = [
                    key
                    for key in state
                    if key not in live_keys and (person is None or key[0] == person)
                ]

                held = self._held[name]
                # A document that vanished, or that came back at a new revision,
                # is no longer the document that left no row.
                for key in [k for k in held if k not in live_keys]:
                    del held[key]
                if full:
                    # An operator asked for the whole source. Try them again.
                    held.clear()

                def _is_held(doc: Document) -> bool:
                    entry = held.get((doc.person_id, doc.uri))
                    return entry is not None and entry[0] == doc.rev

                skipped = [d for d in changed if _is_held(d)]
                changed = [d for d in changed if not _is_held(d)]

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
                        chunks = await self._index_document(doc)
                        indexed += 1
                        key = (doc.person_id, doc.uri)
                        if chunks:
                            held.pop(key, None)
                        else:
                            # No chunk means no row, and the diff reads a
                            # missing row as a changed document. Hold the
                            # revision, or this repeats on every pass.
                            if key not in held:
                                logger.info(
                                    {
                                        "message": "kb document has nothing to index",
                                        "source": name,
                                        "path": doc.uri,
                                        "person": doc.person_id,
                                    }
                                )
                            held[key] = (doc.rev, "empty")
                    except Exception as e:  # noqa: BLE001 — one bad doc must not sink the pass
                        failed += 1
                        if not embed_ok:
                            first_error = first_error or str(e)
                            first_path = first_path or doc.uri
                            continue
                        unindexable = _is_unindexable(e)
                        if unindexable:
                            held[(doc.person_id, doc.uri)] = (doc.rev, "unindexable")
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
                    "unindexable": sum(1 for _, reason in held.values() if reason == "unindexable"),
                    "empty": sum(1 for _, reason in held.values() if reason == "empty"),
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
