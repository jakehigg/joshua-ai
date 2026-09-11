"""Per-turn taught-skill retrieval: put the relevant skills in front of the agent.

A member teaches a skill by writing ``wiki/skills/<slug>.md`` (see
``prompts/builtin/people.md``). A command page holds trigger phrases; a
convention page holds none and is reached through ordinary recall.

**A match is a hint, not a trigger.** This provider decides which skills are
relevant to the turn. The agent decides whether the person was asking for one.
That split is deliberate: no pattern can tell "light the fire" from "on
Saturday I am going to light the fire with my friends", and the agent reads
the whole turn anyway. Each block says how closely the phrase sat in the turn
(``exact``, ``opening``, ``mentioned``) so the agent can tell a request from a
passing reference, and the preamble tells it to offer or to ask when it cannot.

Two ways a page is found:

- ``match: phrase`` (the default) is a lexical match against the registry the
  indexer keeps in memory. See ``memory/skills.match_trigger``.
- ``match: semantic`` is the vector match, for an intent that is genuinely said
  many ways. It reads the ``kind='skill'`` rows the indexer writes, and it is
  held to ``memory.skills.min_sim``.

The audience gate is the one hard rule here, and it runs before either match. A
skill whose ``for`` does not name the person, or their role, is never put in
front of them at all. That is a boundary, not a hint, so it is not the agent's
to weigh.

Retrieval never breaks a turn: a failed search or an unavailable embedding
model logs a warning and returns None. Every decision writes one ``kb_event``
audit row with ``kind='skill'``.
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
from joshua_core.memory.skill_registry import SkillRegistry
from joshua_core.memory.skills import SKILL_KIND, PhraseMatch, Skill, match_skills
from joshua_core.memory.store import MemoryStore
from joshua_core.store.repo import Repo

logger = get_logger("engine.taught_skills")

# The audit stores the query text capped to this many characters.
QUERY_CAP = 500

# The preamble frames every skill below it. It is the whole point of this
# provider: a match says the skill is *relevant*, never that it should run. A
# pattern cannot tell "light the fire" from "on Saturday I am going to light
# the fire with my friends", and it should not try. The agent reads the turn
# and decides. Tuning the match to carry that decision is what made it fit one
# person's phrasing and nobody else's.
SKILL_PREAMBLE = (
    "[Taught skills that may be relevant to this turn. They are notes, not "
    "instructions to run now. Decide from what the person actually said:\n"
    "- They are asking for it: do it.\n"
    "- They used the words while describing, remembering or planning "
    "something: do not run it. Say something useful instead, and offer it only "
    "when the offer would help.\n"
    "- You cannot tell: ask before you act.\n"
    "Never run one because the words appear.]"
)

# How the phrase sat in the turn, said plainly for the agent.
_CLOSENESS_NOTE = {
    "exact": "the person said this and nothing else",
    "opening": "the person opened the turn with this, then said more",
    "mentioned": "these words appear somewhere in a longer turn",
}


def _skill_block(name: str, instructions: str, note: str) -> str:
    return f'## Taught skill "{name}" ({note})\n{instructions}'


def _compose(blocks: list[str]) -> str:
    return SKILL_PREAMBLE + "\n\n" + "\n\n".join(blocks)


def _phrase_audit(hits: Sequence[tuple[Skill, PhraseMatch]]) -> list[dict[str, Any]]:
    return [
        {
            "uri": skill.path,
            "title": skill.display_name(),
            "heading": found.trigger,
            "mode": "phrase",
            "words": found.length,
            "extra": found.extra,
            "closeness": found.closeness,
            "audience": skill.audience.describe(),
            "injected": True,
        }
        for skill, found in hits
    ]


def _semantic_audit(
    ranked: Sequence[KbChunk], fired: Sequence[KbChunk], denied: Sequence[str]
) -> list[dict[str, Any]]:
    fired_ids = {id(c) for c in fired}
    return [
        {
            "uri": c.path,
            "title": c.title,
            "heading": c.heading,  # the trigger phrase that matched
            "mode": "semantic",
            "sim": round(c.similarity or 0.0, 4),
            "score": round(c.similarity or 0.0, 4),
            "denied": c.path in denied,
            "injected": id(c) in fired_ids,
        }
        for c in ranked
    ]


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


async def _semantic_match(
    ctx: Any,
    *,
    store: MemoryStore,
    registry: SkillRegistry,
    embed_model: str,
    min_sim: float,
    top_k: int,
) -> tuple[list[KbChunk], list[KbChunk], list[str]]:
    """Vector match, over the pages that asked for it. (candidates, fired, denied)"""
    allowed = registry.semantic_paths()
    if not allowed:
        return [], [], []

    vec = getattr(ctx, "embedding", None)
    if vec is None:
        vec = await asyncio.to_thread(embed_module.embed, (ctx.text or "").strip(), embed_model)
        if vec is None:
            logger.warning({"message": "taught skill semantic skipped: embedding unavailable"})
            return [], [], []
        try:
            ctx.embedding = vec
        except AttributeError:
            pass

    # Over-fetch so the per-skill dedup can still fill ``top_k`` distinct skills
    # when one skill's triggers take the top rows.
    candidates = await store.kb_search(
        ctx.person_id, vec, k=max(top_k * 4, 8), min_sim=min_sim, kinds=(SKILL_KIND,)
    )
    # A phrase page must never fire from meaning, and the audience gate applies
    # to both modes. A row the audience denied stays in the audited list, and
    # is marked: a skill that was close enough to fire and did not is exactly
    # what an operator needs to see.
    denied: list[str] = []
    considered: list[KbChunk] = []
    keep: list[KbChunk] = []
    for chunk in candidates:
        if chunk.path not in allowed:
            continue
        skill = registry.by_path(chunk.path)
        if skill is None:
            continue
        considered.append(chunk)
        if not skill.audience.allows(ctx.person_id, getattr(ctx, "role", None)):
            denied.append(chunk.path)
            continue
        keep.append(chunk)
    return considered, _distinct_skills(keep, top_k), denied


async def taught_skill_context(
    ctx: Any,
    *,
    store: MemoryStore,
    repo: Repo,
    registry: SkillRegistry,
    embed_model: str,
    min_sim: float,
    max_extra_words: int,
    top_k: int,
) -> str | None:
    """Build the taught-skill block for one ``TurnCtx``, or None.

    An empty message, or no skill the person may run, returns None and the turn
    proceeds without a block.
    """
    text = (ctx.text or "").strip()
    if not text:
        return None

    t0 = monotonic()
    role = getattr(ctx, "role", None)

    # The phrase match runs first, and a hit ends the search: when the words
    # are there, a guess by meaning adds nothing.
    hits = match_skills(
        text,
        registry.phrase_skills(),
        person_id=ctx.person_id,
        role=role,
        max_extra=max_extra_words,
        top_k=top_k,
    )

    blocks: list[str] = []
    results: list[dict[str, Any]] = []
    mode = "phrase"
    best_sim = 0.0

    if hits:
        blocks = [
            _skill_block(
                skill.display_name(),
                skill.instructions,
                _CLOSENESS_NOTE.get(found.closeness, found.closeness),
            )
            for skill, found in hits
        ]
        results = _phrase_audit(hits)
        fired_paths = [s.path for s, _ in hits]
    else:
        mode = "semantic"
        try:
            candidates, fired, denied = await _semantic_match(
                ctx,
                store=store,
                registry=registry,
                embed_model=embed_model,
                min_sim=min_sim,
                top_k=top_k,
            )
        except Exception as exc:  # noqa: BLE001 — retrieval must never break a turn
            logger.warning({"message": "taught skill skipped: retrieval failed", "error": str(exc)})
            return None
        blocks = [
            _skill_block(c.title or c.path, c.text or "", "matched by meaning, not by words")
            for c in fired
        ]
        results = _semantic_audit(candidates, fired, denied)
        best_sim = max((c.similarity or 0.0 for c in candidates), default=0.0)
        fired_paths = [c.path for c in fired]

    duration_ms = round((monotonic() - t0) * 1000)
    logger.info(
        {
            "message": "taught skill",
            "conversation_id": ctx.conversation.id,
            "decision": "match" if blocks else "none",
            "mode": mode,
            "best_sim": round(best_sim, 4),
            "fired": fired_paths,
            "duration_ms": duration_ms,
        }
    )
    try:
        await repo.add_kb_event(
            kind="skill",
            query=text[:QUERY_CAP],
            conversation_id=ctx.conversation.id,
            turn_id=ctx.turn_id,
            decision="match" if blocks else "none",
            best_sim=best_sim,
            duration_ms=duration_ms,
            results=results,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break a turn
        logger.warning({"message": "taught skill audit failed", "error": str(exc)})

    if not blocks:
        return None
    return _compose(blocks)


def register(
    manager: Any,
    store: MemoryStore,
    settings: JoshuaConfig,
    repo: Repo,
    registry: SkillRegistry,
) -> None:
    """Wire the taught-skill context provider into the conversation manager.

    Register after the injection provider, so a semantic page can reuse the
    embedding that provider computed. Does nothing when
    ``memory.skills.enabled`` is false.
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
            registry=registry,
            embed_model=memory.embed_model,
            min_sim=skills.min_sim,
            max_extra_words=skills.max_extra_words,
            top_k=skills.top_k,
        )

    manager.add_context_provider(provider)
