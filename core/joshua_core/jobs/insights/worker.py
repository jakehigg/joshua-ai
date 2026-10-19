"""The `insight` worker: where what we do disagrees with what is known.

The worker has no tools and a timeout of 120 seconds. It reads the parts of
the memory bundle that say what we do, the full text of each published
knowledge page of the topic, the day, the timezone, the instance name, and
the open insights of the topic. The gate in `gate.py` then decides which
insights are written.

The model is asked for the `InsightAnswer` schema. Python checks each item of
the answer on its own, so an item that does not match the schema is a
rejection with a reason, and the other items still count.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from joshua_core.engine.ephemeral import Runner, run_worker
from joshua_core.engine.usage import Usage
from joshua_core.jobs.insights.knowledge import KnownPage
from joshua_core.jobs.insights.page import InsightFile
from joshua_core.jobs.insights.prompts import INSIGHT_PROMPT
from joshua_core.jobs.research.text import Part, inline

WORKER = "insight"
TIMEOUT_S = 120.0
# What one answer may hold.
MAX_RAISED = 20


class RaisedInsight(BaseModel):
    """One insight as the worker gives it."""

    claim: str = Field(max_length=1000)
    why: str = Field(max_length=4000)
    action: str = Field(max_length=4000)
    severity: Literal["note", "act", "urgent"]
    valid_from: str | None = Field(default=None, max_length=40)
    valid_until: str | None = Field(default=None, max_length=40)
    we_do: list[str] = Field(default_factory=list, max_length=12)
    we_do_paths: list[str] = Field(default_factory=list, max_length=12)
    knowledge_says: list[str] = Field(default_factory=list, max_length=12)
    confidence: Literal["low", "medium", "high"]
    evidence: str = Field(default="", max_length=8000)


class InsightAnswer(BaseModel):
    """The answer schema the worker is asked for."""

    insights: list[RaisedInsight] = Field(default_factory=list, max_length=MAX_RAISED)


class _Loose(BaseModel):
    """The answer as Python reads it first: a list of items, each checked on
    its own. Its schema is the schema of `InsightAnswer`."""

    insights: list[Any] = Field(default_factory=list, max_length=MAX_RAISED)

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return InsightAnswer.model_json_schema(*args, **kwargs)


@dataclass(frozen=True)
class Raised:
    """What one call gave: the items that match the schema, the reason for
    each item that does not, or `candidates is None` when there was no answer."""

    candidates: list[RaisedInsight] | None
    invalid: list[tuple[str, str]] = field(default_factory=list)


def _preview(item: Any) -> str:
    if isinstance(item, dict) and isinstance(item.get("claim"), str):
        return inline(item["claim"], 200)
    return "(no claim)"


def insight_text(
    *,
    topic: str,
    title: str,
    parts: Sequence[Part],
    known: Sequence[KnownPage],
    open_insights: Sequence[InsightFile],
    today: date,
    timezone: str,
    instance: str,
) -> str:
    """The user text of the `insight` worker."""
    opened = (
        "\n".join(
            f"- {inline(f.insight.claim, 300)} (status {f.insight.status}, valid until "
            f"{f.insight.valid_until.isoformat()})"
            for f in open_insights
        )
        or "- none"
    )
    pages = "\n\n".join(f"## {page.rel}\n\n{page.text.strip()}" for page in known)
    bundle = "\n\n".join(f"## {part.path}\n\n{part.text}" for part in parts)
    return (
        f"Today: {today.isoformat()} ({today:%A, %B})\n"
        f"Timezone: {timezone}\n"
        f"Instance: {instance}\n"
        f"Topic: {topic} ({title})\n\n"
        f"<open_insights>\n{opened}\n</open_insights>\n\n"
        f"<knowledge>\n{pages}\n</knowledge>\n\n"
        f"<bundle>\n{bundle}\n</bundle>"
    )


async def raise_insights(
    *,
    user_text: str,
    model: str | None,
    runner: Runner | None = None,
    usage_out: list[Usage] | None = None,
) -> Raised:
    """Ask the `insight` worker once."""
    answer = await run_worker(
        name=f"insights-{WORKER}",
        system_prompt=INSIGHT_PROMPT,
        user_text=user_text,
        answer=_Loose,
        model=model,
        timeout_s=TIMEOUT_S,
        runner=runner,
        usage_out=usage_out,
    )
    if answer is None:
        return Raised(None)
    good: list[RaisedInsight] = []
    invalid: list[tuple[str, str]] = []
    for item in answer.insights:
        try:
            good.append(RaisedInsight.model_validate(item))
        except ValidationError as exc:
            invalid.append(
                (
                    _preview(item),
                    f"the answer does not match the schema ({exc.error_count()} errors)",
                )
            )
    return Raised(good, invalid)
