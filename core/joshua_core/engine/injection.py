"""Per-turn KB injection: the confidence-gated RAG note prepended to a turn.

Before a turn runs, :func:`kb_context` embeds the incoming text, searches the
person's scope of the in-core index (own documents plus shared), and
:func:`plan_injection` decides ``full`` / ``hint`` / ``none``:

- ``full``  — the top match clears ``full_sim``; the top ``top_k`` chunk bodies
  go into the turn, so the model answers with zero tool calls.
- ``hint``  — the best match is only above ``hint_sim``; the note names up to
  three documents so the model can open one with ``read_file``.
- ``none``  — nothing cleared ``hint_sim``; no note.

A ``snapshot`` chunk (the state of a live system, ``memory.snapshots``) is
never shown in full. It takes one slot of ``top_k`` and is one short line
under a header that says to check the live tool before a change. The other
chunks get the same decision as on a turn with no snapshot.

The pages that the jobs write are not memory, so a chunk of the kinds
``knowledge``, ``insight``, and ``question`` never reaches a turn this way
(``JOB_KINDS``). In place of an insight or a question, a turn that a person
sent gets one short line when it is about a topic with due insights or open
questions (``jobs/open_items.py``). The line holds only the counts. Every
decision writes one ``kb_event`` audit row, and the row names the topics of
the line. Retrieval never breaks a turn:
an unavailable embedding model or a failed search skips the note and logs a
warning. The provider is registered on the manager's context-provider hook.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from joshua_shared.attachments import is_no_caption
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger

from joshua_core.jobs.insights.page import local_day
from joshua_core.jobs.open_items import OpenItems, TopicItems, hint_line
from joshua_core.jobs.retrieval import INSIGHT_KIND, QUESTION_KIND, ChatFilter
from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk
from joshua_core.memory.search import search
from joshua_core.memory.skills import SKILL_KIND
from joshua_core.memory.sources.snapshots import item_body, shown, snapshot_labels
from joshua_core.memory.store import SNAPSHOT_KIND, MemoryStore
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
SNAPSHOT_HEADER = (
    "[Snapshots of live systems, as of the time on each line. They can be out of "
    "date: call the live tool to check before you change anything:]"
)
# A snapshot line holds at most this many characters of the item.
SNAPSHOT_LINE_CHARS = 200
# The kinds of the pages that the jobs write. They are not memory, and the
# per-turn search leaves them out.
KNOWLEDGE_KIND = "knowledge"
JOB_KINDS = (KNOWLEDGE_KIND, INSIGHT_KIND, QUESTION_KIND)
# The direction of a turn that a person sent. A scheduled task is `task`,
# and an event or a webhook is `inject`.
PERSON_DIRECTION = "in"


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
    if best_sim >= inject_sim and top_k > 0:
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


def _snapshot_line(chunk: KbChunk, labels: dict[str, str]) -> str:
    """``<name> as of <time>: <item>`` on one line, the item cut to
    ``SNAPSHOT_LINE_CHARS``. The time is the latest good fetch, from
    ``labels``."""
    head = labels.get(chunk.title, chunk.title)
    item = " ".join(item_body(chunk.text or "").split())[:SNAPSHOT_LINE_CHARS]
    return f"{head}: {item}" if item else head


def _snapshot_note(chunks: Sequence[KbChunk], labels: dict[str, str]) -> str:
    lines = "\n".join(_snapshot_line(c, labels) for c in chunks)
    return SNAPSHOT_HEADER + "\n" + lines


def split_snapshots(ranked: Sequence[KbChunk], top_k: int) -> tuple[list[KbChunk], list[KbChunk]]:
    """``(snapshots, others)``: the snapshot chunks among the top ``top_k``,
    and every chunk that is not a snapshot, in rank order."""
    snaps = [c for c in ranked[:top_k] if c.kind == SNAPSHOT_KIND]
    others = [c for c in ranked if c.kind != SNAPSHOT_KIND]
    return snaps, others


def _audit_results(
    ranked: Sequence[KbChunk], injected: Sequence[KbChunk], max_chars: int
) -> list[dict[str, Any]]:
    injected_ids = {id(c) for c in injected}
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
    keep: Callable[[KbChunk], bool] | None = None,
    timezone: str = "UTC",
    open_items: OpenItems | None = None,
) -> str | None:
    """Build the per-turn injection note for one ``TurnCtx``, or None.

    ``keep`` drops a candidate chunk before the top-k trim. A snapshot line
    shows the time of the latest good fetch in ``timezone``, read once for the
    turn; a snapshot chunk with no good fetch is dropped. With
    ``open_items``, a turn that a person sent gets the topic hint line.

    A message shorter than :data:`MIN_QUERY_CHARS`, an unavailable embedding
    model, or a failed retrieval returns None with no audit row (the turn
    proceeds without a note).
    """
    text = (ctx.text or "").strip()
    if len(text) < MIN_QUERY_CHARS or is_no_caption(text):
        # The no-caption placeholder is the same words on every such turn, so a
        # search with it returns whatever sits nearest that sentence.
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
            exclude_kinds=(SKILL_KIND, *JOB_KINDS),
            recency_bonus=recency_bonus,
            recency_half_life_days=recency_half_life_days,
            per_doc_cap=per_doc_cap,
            keep=keep,
        )
    except Exception as exc:  # noqa: BLE001 — retrieval must never break a turn
        logger.warning({"message": "kb inject skipped: retrieval failed", "error": str(exc)})
        return None

    labels = await snapshot_labels(store, ranked, timezone)
    ranked = shown(ranked, labels)
    topics = _open_topics(ctx, text, ranked, open_items)
    # A snapshot chunk takes a slot of `top_k` and is always a short line. The
    # other chunks get the same decision as on a turn with no snapshot.
    snaps, others = split_snapshots(ranked, top_k)
    plan = plan_injection(others, inject_sim=full_sim, top_k=top_k - len(snaps))
    decision = plan.decision
    if snaps and decision == "none":
        decision = "snapshot"
    duration_ms = round((monotonic() - t0) * 1000)
    logger.info(
        {
            "message": "kb inject",
            "conversation_id": ctx.conversation.id,
            "decision": decision,
            "best_sim": round(plan.best_sim, 4),
            "results": len(ranked),
            "snapshots": len(snaps),
            "topics": len(topics),
            "duration_ms": duration_ms,
        }
    )
    try:
        await repo.add_kb_event(
            kind="inject",
            query=text[:QUERY_CAP],
            conversation_id=ctx.conversation.id,
            turn_id=ctx.turn_id,
            decision=decision,
            best_sim=plan.best_sim,
            duration_ms=duration_ms,
            results=_audit_results(ranked, [*plan.injected, *snaps], max_chars),
            topics=[item.audit() for item in topics],
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break a turn
        logger.warning({"message": "kb inject audit failed", "error": str(exc)})

    notes: list[str] = []
    if plan.decision == "full":
        notes.append(_full_note(plan.injected, max_chars))
    elif plan.decision == "hint":
        notes.append(_hint_note(plan.injected))
    if snaps:
        notes.append(_snapshot_note(snaps, labels))
    if topics:
        notes.append(hint_line(topics))
    return "\n\n".join(notes) or None


def is_person_turn(ctx: Any) -> bool:
    """True for a turn that a person sent: not a scheduled task, an event, or
    a webhook."""
    return getattr(ctx, "direction", PERSON_DIRECTION) == PERSON_DIRECTION


def _open_topics(
    ctx: Any, text: str, ranked: Sequence[KbChunk], open_items: OpenItems | None
) -> list[TopicItems]:
    """The topics of the hint line of this turn. It never breaks a turn."""
    if open_items is None or not is_person_turn(ctx):
        return []
    try:
        return open_items.about(text, [c.path for c in ranked])
    except Exception as exc:  # noqa: BLE001 — the hint must never break a turn
        logger.warning({"message": "topic hint skipped", "error": str(exc)})
        return []


def register(
    manager: Any,
    store: MemoryStore,
    settings: JoshuaConfig,
    repo: Repo,
    *,
    data_dir: Path | str,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Wire the injection context provider into the conversation manager.

    Each turn gets a new ``ChatFilter`` for the day of ``clock`` (the
    instance timezone). With ``jobs.enabled``, one ``OpenItems`` serves the
    topic hint of every turn: it counts the insights when the insights job is
    on, and the questions when the researcher is on. Does nothing when
    ``memory.inject.enabled`` is false.
    """
    memory = settings.memory
    inject = memory.inject
    if not inject.enabled:
        logger.info({"message": "kb injection disabled"})
        return

    now = clock or _utc_now
    open_items = _open_items(settings, data_dir, now)

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
            keep=ChatFilter(data_dir, local_day(now(), settings)),
            timezone=getattr(settings, "timezone", "UTC"),
            open_items=open_items,
        )

    manager.add_context_provider(provider)


def _open_items(
    settings: JoshuaConfig, data_dir: Path | str, now: Callable[[], datetime]
) -> OpenItems | None:
    """The `OpenItems` of the topic hint, or None when no job writes an
    insight or a question."""
    jobs = getattr(settings, "jobs", None)
    if jobs is None or not jobs.enabled:
        return None
    if not (jobs.insights.enabled or jobs.research.enabled):
        return None
    return OpenItems(
        data_dir,
        today=lambda: local_day(now(), settings),
        people=settings.people,
        insights=jobs.insights.enabled,
        questions=jobs.research.enabled,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)
