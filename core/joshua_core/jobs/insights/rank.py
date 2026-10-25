"""The verdict examples, the open budget, and the `rank` worker.

**The verdict examples.** The people of an instance keep an insight
(`told` or `acknowledged`) or dismiss it with a reason. `verdict_examples`
gives the most recent of each, from every topic, so the `insight` worker
and the `rank` worker see what these people find worth telling. Only the
claims, and the reason of a dismissal, go to a worker.

**The open budget.** `jobs.insights.max_open` is the most open insights
(`new`, `told`, `acknowledged`; a snoozed insight counts) of the whole
instance. When the open insights and the new ones of a run are more than
that, the tool-less `rank` worker gets each item and the verdict examples,
and answers with the ids in order of importance. The first `max_open` stay.
A new insight below the line is rejected with `below the open budget`. An
open insight below the line becomes `expired` with `resolved_by: rank`, and
a later run can raise it again. An id that the worker leaves out goes to the
end, in the old order. When the worker gives no answer, every open insight
stays, and the new ones over the budget are rejected with
`rank worker gave no answer`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from pydantic import Field

from joshua_core.engine.ephemeral import Runner, run_worker
from joshua_core.engine.sanitize import CleanModel
from joshua_core.engine.usage import Usage
from joshua_core.jobs.insights.page import InsightFile
from joshua_core.jobs.insights.prompts import RANK_PROMPT
from joshua_core.jobs.insights.worker import dismissed_lines, kept_lines
from joshua_core.jobs.research.text import inline

WORKER = "rank"
TIMEOUT_S = 60.0
# The resolver of an open insight that the rank put below the line.
RANKED_OUT = "rank"
BELOW_BUDGET = "below the open budget"
NO_RANK = "rank worker gave no answer"
KEPT_STATUSES = ("told", "acknowledged")
MAX_ITEMS = 200


def _kept_day(item: InsightFile) -> date:
    i = item.insight
    return i.resolved_at or i.told_at or i.seen_at


def _dismissed_day(item: InsightFile) -> date:
    i = item.insight
    return i.resolved_at or i.seen_at


def verdict_examples(
    insights: Sequence[InsightFile], limit: int
) -> tuple[list[InsightFile], list[InsightFile]]:
    """`(kept, dismissed)`: the most recent `limit` insights that a person
    kept, and the most recent `limit` that a person dismissed, newest
    first."""
    if limit <= 0:
        return [], []
    kept = [f for f in insights if f.insight.status in KEPT_STATUSES]
    dismissed = [f for f in insights if f.insight.status == "dismissed"]
    kept.sort(key=lambda f: (_kept_day(f), f.id), reverse=True)
    dismissed.sort(key=lambda f: (_dismissed_day(f), f.id), reverse=True)
    return kept[:limit], dismissed[:limit]


@dataclass(frozen=True)
class RankItem:
    """One insight as the rank worker reads it. `new` is True for an
    insight that this run found."""

    id: str
    topic: str
    claim: str
    why: str
    status: str
    new: bool = False


class RankAnswer(CleanModel):
    """The answer schema of the rank worker."""

    order: list[str] = Field(default_factory=list, max_length=MAX_ITEMS)


def rank_text(
    items: Sequence[RankItem], kept: Sequence[InsightFile], dismissed: Sequence[InsightFile]
) -> str:
    """The user text of the rank worker."""
    lines = "\n".join(
        f"- id: {item.id}; topic: {item.topic}; status: {item.status}; claim: "
        f"{inline(item.claim, 300)}; why: {inline(item.why, 400) or 'not given'}"
        for item in items
    )
    return (
        f"<insights>\n{lines}\n</insights>\n\n"
        f"<kept>\n{kept_lines(kept)}\n</kept>\n\n"
        f"<dismissed>\n{dismissed_lines(dismissed)}\n</dismissed>"
    )


def full_order(answer: Sequence[str], old: Sequence[str]) -> list[str]:
    """Each id of `old` once: in the order of `answer`, then the ids that
    `answer` leaves out, in the order of `old`. An id that is not in `old`
    does not count."""
    known = set(old)
    out: list[str] = []
    for value in answer:
        key = value.strip()
        if key in known and key not in out:
            out.append(key)
    out.extend(key for key in old if key not in out)
    return out


async def rank_items(
    items: Sequence[RankItem],
    *,
    kept: Sequence[InsightFile],
    dismissed: Sequence[InsightFile],
    model: str | None,
    runner: Runner | None = None,
    usage_out: list[Usage] | None = None,
) -> list[str] | None:
    """Ask the rank worker once. The full order of the ids, or None when the
    worker gave no answer."""
    answer = await run_worker(
        name=f"insights-{WORKER}",
        system_prompt=RANK_PROMPT,
        user_text=rank_text(items, kept, dismissed),
        answer=RankAnswer,
        model=model,
        timeout_s=TIMEOUT_S,
        runner=runner,
        usage_out=usage_out,
    )
    if answer is None:
        return None
    return full_order(answer.order, [item.id for item in items])


@dataclass
class Budget:
    """What the open budget decided.

    `order` is the full order when the rank worker answered, else empty.
    `rejected` holds `(id, reason)` for each new insight that does not stay.
    `ranked_out` holds the ids of the open insights below the line.
    """

    order: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    ranked_out: list[str] = field(default_factory=list)
    asked: bool = False


def apply_budget(items: Sequence[RankItem], max_open: int, order: list[str] | None) -> Budget:
    """The decision of the budget for `items`: the open insights first, then
    the new ones. `order` is the answer of the rank worker, or None when it
    gave none."""
    if len(items) <= max_open:
        return Budget()
    new_ids = [item.id for item in items if item.new]
    if order is None:
        room = max(0, max_open - (len(items) - len(new_ids)))
        return Budget(rejected=[(i, NO_RANK) for i in new_ids[room:]], asked=True)
    below = order[max_open:]
    new = set(new_ids)
    return Budget(
        order=list(order),
        rejected=[(i, BELOW_BUDGET) for i in below if i in new],
        ranked_out=[i for i in below if i not in new],
        asked=True,
    )


__all__ = [
    "BELOW_BUDGET",
    "NO_RANK",
    "RANKED_OUT",
    "WORKER",
    "Budget",
    "RankAnswer",
    "RankItem",
    "apply_budget",
    "full_order",
    "rank_items",
    "rank_text",
    "verdict_examples",
]
