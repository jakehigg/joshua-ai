"""Per-turn taught-skill matching: fire a skill whose trigger matches the turn.

A member teaches a skill by writing ``wiki/skills/<slug>.md`` (see
``prompts/builtin/people.md``). The indexer writes one ``kind='skill'`` row per
trigger phrase. This provider embeds the turn text (or reuses the vector the
injection provider already computed), searches only the skill rows, and prepends
one instruction block per matched skill.

The match is phrase against phrase with no recency bonus — a trigger phrase has
no age. Retrieval never breaks a turn: a failed search or an unavailable
embedding model logs a warning and returns None. Every decision writes one
``kb_event`` audit row with ``kind='skill'``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from time import monotonic
from typing import Any

from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk
from joshua_core.memory.skills import SKILL_KIND, skill_slug
from joshua_core.memory.store import MemoryStore
from joshua_core.store.repo import Repo

logger = get_logger("engine.taught_skills")

# The audit stores the query text capped to this many characters.
QUERY_CAP = 500

SKILL_HEADER = '[Taught skill "{name}" matches this request. Follow its instructions now:]'


def _distinct_skills(ranked: Sequence[KbChunk], top_k: int) -> list[KbChunk]:
    """The best chunk of each distinct skill file, in rank order, up to ``top_k``.

    Two triggers of one skill can both match; a skill fires once, so keep only
    the first (best) row per path.
    """
    seen: set[str] = set()
    out: list[KbChunk] = []
    for chunk in ranked:
        if chunk.path in seen:
            continue
        seen.add(chunk.path)
        out.append(chunk)
        if len(out) >= top_k:
            break
    return out


def _skill_block(chunk: KbChunk) -> str:
    name = chunk.title or skill_slug(chunk.path) or "skill"
    return SKILL_HEADER.format(name=name) + "\n" + (chunk.text or "")


def _audit_results(ranked: Sequence[KbChunk], fired: Sequence[KbChunk]) -> list[dict[str, Any]]:
    fired_ids = {id(c) for c in fired}
    return [
        {
            "uri": c.path,
            "title": c.title,
            "heading": c.heading,  # the trigger phrase that matched
            "sim": round(c.similarity or 0.0, 4),
            "score": round(c.similarity or 0.0, 4),
            "injected": id(c) in fired_ids,
        }
        for c in ranked
    ]


async def taught_skill_context(
    ctx: Any,
    *,
    store: MemoryStore,
    repo: Repo,
    embed_model: str,
    min_sim: float,
    top_k: int,
) -> str | None:
    """Build the taught-skill block for one ``TurnCtx``, or None.

    An empty message, an unavailable embedding model, or a failed retrieval
    returns None (the turn proceeds without a block).
    """
    text = (ctx.text or "").strip()
    if not text:
        return None

    t0 = monotonic()
    vec = getattr(ctx, "embedding", None)
    if vec is None:
        vec = await asyncio.to_thread(embed_module.embed, text, embed_model)
        if vec is None:
            logger.warning({"message": "taught skill skipped: embedding unavailable"})
            return None
        try:
            ctx.embedding = vec
        except AttributeError:
            pass

    try:
        # Over-fetch so the per-skill dedup can still fill ``top_k`` distinct
        # skills when one skill's triggers take the top rows.
        candidates = await store.kb_search(
            ctx.person_id,
            vec,
            k=max(top_k * 4, 8),
            min_sim=min_sim,
            kinds=(SKILL_KIND,),
        )
    except Exception as exc:  # noqa: BLE001 — retrieval must never break a turn
        logger.warning({"message": "taught skill skipped: retrieval failed", "error": str(exc)})
        return None

    fired = _distinct_skills(candidates, top_k)
    best_sim = max((c.similarity or 0.0 for c in candidates), default=0.0)
    duration_ms = round((monotonic() - t0) * 1000)
    logger.info(
        {
            "message": "taught skill",
            "conversation_id": ctx.conversation.id,
            "decision": "match" if fired else "none",
            "best_sim": round(best_sim, 4),
            "fired": [c.path for c in fired],
            "duration_ms": duration_ms,
        }
    )
    try:
        await repo.add_kb_event(
            kind="skill",
            query=text[:QUERY_CAP],
            conversation_id=ctx.conversation.id,
            turn_id=ctx.turn_id,
            decision="match" if fired else "none",
            best_sim=best_sim,
            duration_ms=duration_ms,
            results=_audit_results(candidates, fired),
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break a turn
        logger.warning({"message": "taught skill audit failed", "error": str(exc)})

    if not fired:
        return None
    return "\n\n".join(_skill_block(c) for c in fired)


def register(manager: Any, store: MemoryStore, settings: JoshuaConfig, repo: Repo) -> None:
    """Wire the taught-skill context provider into the conversation manager.

    Register after the injection provider, so the shared embedding is set. Does
    nothing when ``memory.skills.enabled`` is false.
    """
    memory = settings.memory
    skills = memory.skills
    if not skills.enabled:
        logger.info({"message": "taught skills disabled"})
        return

    async def provider(ctx: Any) -> str | None:
        return await taught_skill_context(
            ctx,
            store=store,
            repo=repo,
            embed_model=memory.embed_model,
            min_sim=skills.min_sim,
            top_k=skills.top_k,
        )

    manager.add_context_provider(provider)
