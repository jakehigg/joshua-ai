"""In-process ``recall`` tool server: search the caller's memory.

One tool, ``search_memory``, runs a person-scoped RAG search over the indexed
data volume: the wiki (including the journal) plus any of the caller's own
documents from another source (a memo, for example). A group turn
(``deps.person_id`` None) sees shared documents only. Read-only — each result
carries the ``path`` the agent can open with the ``files`` tool ``read_file``.
A result is a snippet of at most ``SNIPPET_CHARS`` characters unless the call
asks for ``full``. A ``snapshot`` result has no file: in place of the path, it
names the live tool that holds the current data.
The kinds ``insight`` and ``question`` are left out (``EXCLUDED_KINDS``):
``list_insights`` and ``list_questions`` read them. The kind ``knowledge``
stays, so the agent can search the facts that the researcher found.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from joshua_shared.config import JoshuaConfig, Snapshot
from joshua_shared.log import get_logger

from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.tools import ToolDeps
from joshua_core.jobs.insights.page import local_day
from joshua_core.jobs.retrieval import INSIGHT_KIND, QUESTION_KIND, ChatFilter
from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk
from joshua_core.memory.search import search
from joshua_core.memory.skills import SKILL_KIND
from joshua_core.memory.sources.snapshots import shown, snapshot_labels
from joshua_core.memory.store import SNAPSHOT_KIND, MemoryStore

logger = get_logger("tools.recall")

DEFAULT_LIMIT = 6
MAX_LIMIT = 20
# A result holds at most this many characters of its chunk, unless `full`.
SNIPPET_CHARS = 400
# The kinds a search leaves out: a taught skill, and the insights and the
# questions, which have their own tools.
EXCLUDED_KINDS = (SKILL_KIND, INSIGHT_KIND, QUESTION_KIND)


def _reply(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def snapshot_pointer(
    chunk: KbChunk, snapshots: Mapping[str, Snapshot], labels: Mapping[str, str]
) -> str:
    """``snapshot <name> as of <time>; for live data call <server> <tool>``.
    The time is the latest good fetch, from ``labels``."""
    head = labels.get(chunk.title, chunk.title)
    snap = snapshots.get(chunk.title)
    if snap is None:
        return f"snapshot {head}"
    return f"snapshot {head}; for live data call {snap.server} {snap.tool}"


def _result(
    chunk: KbChunk,
    *,
    full: bool,
    snapshots: Mapping[str, Snapshot],
    labels: Mapping[str, str],
) -> dict[str, Any]:
    text = chunk.text or ""
    out: dict[str, Any] = {"title": chunk.title, "heading": chunk.heading}
    if chunk.kind == SNAPSHOT_KIND:
        out["snapshot"] = snapshot_pointer(chunk, snapshots, labels)
    else:
        out["path"] = chunk.path
    if not full and len(text) > SNIPPET_CHARS:
        out["text"] = text[:SNIPPET_CHARS]
        out["cut"] = True
    else:
        out["text"] = text
    out["score"] = round(chunk.similarity or 0.0, 4)
    return out


async def do_search_memory(
    deps: ToolDeps,
    *,
    store: MemoryStore,
    embed_model: str,
    min_sim: float,
    query: str,
    limit: int = DEFAULT_LIMIT,
    recency_bonus: float = 0.08,
    recency_half_life_days: float = 14.0,
    per_doc_cap: int = 3,
    keep: Callable[[KbChunk], bool] | None = None,
    full: bool = False,
    snapshots: Sequence[Snapshot] = (),
) -> list[dict[str, Any]]:
    """Return ``[{path, title, heading, text, score}]`` for the caller's scope.

    ``text`` is a snippet of at most ``SNIPPET_CHARS`` characters, with
    ``cut: true`` when it is shorter than the chunk; ``full`` returns the whole
    chunk. A snapshot result carries ``snapshot`` (the live tool to call) in
    place of ``path``, with the time of the latest good fetch in ``deps.tz``;
    a snapshot chunk with no good fetch is dropped. ``keep`` drops a
    candidate chunk before the trim to ``limit``. An empty query or an
    unavailable embedding model returns ``[]``.
    """
    query = (query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    vec = await asyncio.to_thread(embed_module.embed, query, embed_model)
    if vec is None:
        logger.warning({"message": "recall unavailable (embedding not loaded)"})
        return []
    chunks = await search(
        store,
        deps.person_id,
        vec,
        k=limit,
        min_sim=min_sim,
        exclude_kinds=EXCLUDED_KINDS,
        recency_bonus=recency_bonus,
        recency_half_life_days=recency_half_life_days,
        per_doc_cap=per_doc_cap,
        keep=keep,
    )
    labels = await snapshot_labels(store, chunks, deps.tz)
    by_name = {snap.name: snap for snap in snapshots}
    return [
        _result(c, full=bool(full), snapshots=by_name, labels=labels) for c in shown(chunks, labels)
    ]


_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What to look for in the memory.",
        },
        "limit": {
            "type": "integer",
            "description": f"Maximum results to return (default {DEFAULT_LIMIT}).",
        },
        "full": {
            "type": "boolean",
            "description": (
                f"True returns the whole text of each passage. The default is a "
                f"snippet of at most {SNIPPET_CHARS} characters."
            ),
        },
    },
    "required": ["query"],
}


def build_recall_server(
    deps: ToolDeps,
    *,
    store: MemoryStore,
    embed_model: str,
    min_sim: float,
    recency_bonus: float,
    recency_half_life_days: float,
    per_doc_cap: int,
    data_dir: Path | str | None = None,
    today: Callable[[], date] | None = None,
    snapshots: Sequence[Snapshot] = (),
) -> Any:
    """Build the in-process ``recall`` MCP server bound to one conversation.

    With ``data_dir`` and ``today``, each search gets a new ``ChatFilter``
    for the day that ``today`` gives."""

    @tool(
        "search_memory",
        "Search the memory (the wiki, your journal, and the shared notes) for text "
        f"relevant to a query. Each result is a snippet of at most {SNIPPET_CHARS} "
        "characters with its title and the file path: open the path with read_file, "
        "or pass full: true for the whole passage. A result with 'snapshot' in place "
        "of a path is the state of a live system at the time it names: call the "
        "tool it names for the current data. The researched facts in knowledge/ are "
        "in the results. Insights and questions for a person are not: read them with "
        "list_insights and list_questions.",
        _SEARCH_SCHEMA,
    )
    async def search_memory(args: dict[str, Any]) -> dict[str, Any]:
        results = await do_search_memory(
            deps,
            store=store,
            embed_model=embed_model,
            min_sim=min_sim,
            query=args.get("query", ""),
            limit=args.get("limit") or DEFAULT_LIMIT,
            recency_bonus=recency_bonus,
            recency_half_life_days=recency_half_life_days,
            per_doc_cap=per_doc_cap,
            keep=ChatFilter(data_dir, today()) if data_dir is not None and today else None,
            full=args.get("full") is True,
            snapshots=snapshots,
        )
        return _reply(json.dumps(results))

    return create_sdk_mcp_server(name="recall", version="1.0.0", tools=[search_memory])


def register(
    manager: Any,
    store: MemoryStore,
    settings: JoshuaConfig,
    *,
    data_dir: Path | str,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Wire the ``recall`` builtin into the conversation manager. A search
    drops the insights and questions that are closed on the day of ``clock``
    (the instance timezone)."""
    memory = settings.memory
    now = clock or (lambda: datetime.now(UTC))

    def today() -> date:
        return local_day(now(), settings)

    def factory(deps: ToolDeps) -> Any:
        return build_recall_server(
            deps,
            store=store,
            embed_model=memory.embed_model,
            min_sim=memory.min_sim,
            recency_bonus=memory.recency_bonus,
            recency_half_life_days=memory.recency_half_life_days,
            per_doc_cap=memory.per_doc_cap,
            data_dir=data_dir,
            today=today,
            snapshots=memory.snapshots,
        )

    manager.register_builtin("recall", factory)
