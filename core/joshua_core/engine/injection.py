"""Per-turn KB injection: the confidence-gated RAG note prepended to a turn.

Before a turn runs, :func:`kb_context` embeds the incoming text, searches the
person's scope of the in-core index (own documents plus shared), and
:func:`plan_injection` decides ``full`` / ``hint`` / ``none``:

- ``full``  — the top match clears ``full_sim``; the top ``top_k`` chunk bodies
  go into the turn, so the model answers with zero tool calls.
- ``hint``  — the best match is only above ``hint_sim``; the note names up to
  three documents so the model can open one with ``read_file``.
- ``none``  — nothing cleared ``hint_sim``; no note.

Every decision writes one ``kb_event`` audit row. Retrieval never breaks a turn:
an unavailable embedding model or a failed search skips the note and logs a
warning. The provider is registered on the manager's context-provider hook.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Any

from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk
from joshua_core.memory.search import search
from joshua_core.memory.skills import SKILL_KIND
from joshua_core.memory.store import MemoryStore
from joshua_core.store.repo import Repo

logger = get_logger("engine.injection")

# Below this length a message is a greeting or an acknowledgement, not a query.
MIN_QUERY_CHARS = 8
# A hint names at most this many distinct documents.
HINT_MAX_DOCS = 3
# The audit stores the query text capped to this many characters.
QUERY_CAP = 500

FULL_HEADER = (
    "[From your notes and files — likely relevant; use directly, or open the file "
    "with read_file for more:]"
)
HINT_HEADER = "[Related notes and files may help — open with read_file if useful: {files}]"


@dataclass
class InjectionPlan:
    """The injection decision for one turn.

    ``injected`` is the chunks placed in the note (empty for ``none``);
    ``best_sim`` is the highest cosine similarity among the candidates.
    """

    decision: str  # "full" | "hint" | "none"
    injected: list[KbChunk]
    best_sim: float


def _distinct_docs(ranked: Sequence[KbChunk], limit: int) -> list[KbChunk]:
    """First chunk of each distinct document, in rank order, up to ``limit``."""
    seen: set[str] = set()
    out: list[KbChunk] = []
    for chunk in ranked:
        if chunk.path in seen:
            continue
        seen.add(chunk.path)
        out.append(chunk)
        if len(out) >= limit:
            break
    return out


def plan_injection(ranked: Sequence[KbChunk], *, inject_sim: float, top_k: int) -> InjectionPlan:
    """Decide ``full`` / ``hint`` / ``none`` over ranked candidates.

    Candidates are already above the hint floor (the search ``min_sim``). When the
    best similarity clears ``inject_sim`` the plan is ``full`` over the top
    ``top_k`` chunks; otherwise ``hint`` over up to three distinct documents. No
    candidates is ``none``.
    """
    if not ranked:
        return InjectionPlan("none", [], 0.0)
    best_sim = max((c.similarity or 0.0) for c in ranked)
    if best_sim >= inject_sim:
        return InjectionPlan("full", list(ranked[:top_k]), best_sim)
    return InjectionPlan("hint", _distinct_docs(ranked, HINT_MAX_DOCS), best_sim)


def _full_note(chunks: Sequence[KbChunk], max_chars: int) -> str:
    blocks = []
    for chunk in chunks:
        label = chunk.title or chunk.path
        blocks.append(f"{label} (read_file: {chunk.path})\n{(chunk.text or '')[:max_chars]}")
    return FULL_HEADER + "\n\n" + "\n\n".join(blocks)


def _hint_note(chunks: Sequence[KbChunk]) -> str:
    files = ", ".join(f"{c.path} ({c.title})" if c.title else c.path for c in chunks)
    return HINT_HEADER.format(files=files)


def _audit_results(
    ranked: Sequence[KbChunk], plan: InjectionPlan, max_chars: int
) -> list[dict[str, Any]]:
    injected_ids = {id(c) for c in plan.injected}
    return [
        {
            "uri": c.path,
            "title": c.title,
            "heading": c.heading,
            "sim": round(c.similarity or 0.0, 4),
            "score": round(c.similarity or 0.0, 4),
            "injected": id(c) in injected_ids,
            "text": (c.text or "")[:max_chars],
        }
        for c in ranked
    ]


async def kb_context(
    ctx: Any,
    *,
    store: MemoryStore,
    repo: Repo,
    embed_model: str,
    full_sim: float,
    hint_sim: float,
    top_k: int,
    max_chars: int,
    recency_bonus: float,
    recency_half_life_days: float,
    per_doc_cap: int,
) -> str | None:
    """Build the per-turn injection note for one ``TurnCtx``, or None.

    A message shorter than :data:`MIN_QUERY_CHARS`, an unavailable embedding
    model, or a failed retrieval returns None with no audit row (the turn
    proceeds without a note).
    """
    text = (ctx.text or "").strip()
    if len(text) < MIN_QUERY_CHARS:
        return None

    t0 = monotonic()
    vec = await asyncio.to_thread(embed_module.embed, text, embed_model)
    if vec is None:
        logger.warning({"message": "kb inject skipped: embedding unavailable"})
        return None
    # Share the vector with the taught-skill provider so one turn embeds once.
    try:
        ctx.embedding = vec
    except AttributeError:
        pass

    try:
        ranked = await search(
            store,
            ctx.person_id,
            vec,
            k=max(6, top_k * 3),
            min_sim=hint_sim,
            exclude_kinds=(SKILL_KIND,),
            recency_bonus=recency_bonus,
            recency_half_life_days=recency_half_life_days,
            per_doc_cap=per_doc_cap,
        )
    except Exception as exc:  # noqa: BLE001 — retrieval must never break a turn
        logger.warning({"message": "kb inject skipped: retrieval failed", "error": str(exc)})
        return None

    plan = plan_injection(ranked, inject_sim=full_sim, top_k=top_k)
    duration_ms = round((monotonic() - t0) * 1000)
    logger.info(
        {
            "message": "kb inject",
            "conversation_id": ctx.conversation.id,
            "decision": plan.decision,
            "best_sim": round(plan.best_sim, 4),
            "results": len(ranked),
            "duration_ms": duration_ms,
        }
    )
    try:
        await repo.add_kb_event(
            kind="inject",
            query=text[:QUERY_CAP],
            conversation_id=ctx.conversation.id,
            turn_id=ctx.turn_id,
            decision=plan.decision,
            best_sim=plan.best_sim,
            duration_ms=duration_ms,
            results=_audit_results(ranked, plan, max_chars),
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break a turn
        logger.warning({"message": "kb inject audit failed", "error": str(exc)})

    if plan.decision == "full":
        return _full_note(plan.injected, max_chars)
    if plan.decision == "hint":
        return _hint_note(plan.injected)
    return None


def register(manager: Any, store: MemoryStore, settings: JoshuaConfig, repo: Repo) -> None:
    """Wire the injection context provider into the conversation manager.

    Does nothing when ``memory.inject.enabled`` is false.
    """
    memory = settings.memory
    inject = memory.inject
    if not inject.enabled:
        logger.info({"message": "kb injection disabled"})
        return

    async def provider(ctx: Any) -> str | None:
        return await kb_context(
            ctx,
            store=store,
            repo=repo,
            embed_model=memory.embed_model,
            full_sim=inject.full_sim,
            hint_sim=inject.hint_sim,
            top_k=inject.top_k,
            max_chars=inject.max_chars,
            recency_bonus=memory.recency_bonus,
            recency_half_life_days=memory.recency_half_life_days,
            per_doc_cap=memory.per_doc_cap,
        )

    manager.add_context_provider(provider)
