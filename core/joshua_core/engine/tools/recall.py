"""In-process ``recall`` tool server: search the caller's memory.

One tool, ``search_memory``, runs a person-scoped RAG search over the indexed
data volume: the wiki (including the journal) plus any of the caller's own
documents from another source (a memo, for example). A group turn
(``deps.person_id`` None) sees shared documents only. Read-only — each result
carries the ``path`` the agent can open with the ``files`` tool ``read_file``.
A chunk of an insight that is not due, or of a question that is not open, is
left out (``jobs/retrieval.py``).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.engine.agent import create_sdk_mcp_server, tool
from joshua_core.engine.tools import ToolDeps
from joshua_core.jobs.insights.page import local_day
from joshua_core.jobs.retrieval import ChatFilter
from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk
from joshua_core.memory.search import search
from joshua_core.memory.skills import SKILL_KIND
from joshua_core.memory.store import MemoryStore

logger = get_logger("tools.recall")

DEFAULT_LIMIT = 6
MAX_LIMIT = 20


def _reply(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


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
) -> list[dict[str, Any]]:
    """Return ``[{path, title, heading, text, score}]`` for the caller's scope.

    ``keep`` drops a candidate chunk before the trim to ``limit``. An empty
    query or an unavailable embedding model returns ``[]``.
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
        exclude_kinds=(SKILL_KIND,),
        recency_bonus=recency_bonus,
        recency_half_life_days=recency_half_life_days,
        per_doc_cap=per_doc_cap,
        keep=keep,
    )
    return [
        {
            "path": c.path,
            "title": c.title,
            "heading": c.heading,
            "text": c.text,
            "score": round(c.similarity or 0.0, 4),
        }
        for c in chunks
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
) -> Any:
    """Build the in-process ``recall`` MCP server bound to one conversation.

    With ``data_dir`` and ``today``, each search gets a new ``ChatFilter``
    for the day that ``today`` gives."""

    @tool(
        "search_memory",
        "Search the memory (the wiki, your journal, and the shared notes) for text "
        "relevant to a query. Returns matching passages with the file path to open.",
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
        )

    manager.register_builtin("recall", factory)
