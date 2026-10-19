"""The `raise` worker: questions about the world, made specific to one topic.

Each question goes to the internet agent, which searches the open web and has
none of our pages, so a question asks what an expert source can answer and
carries our conditions in its words.

The worker reads the memory bundle of the topic, its title and aliases, and
the questions the dossier holds. It has no tools. The specificity gate in
`gate.py` then decides which questions the dossier takes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from joshua_core.engine.ephemeral import Runner, run_worker
from joshua_core.engine.usage import Usage
from joshua_core.jobs.research.gate import Candidate
from joshua_core.jobs.research.prompts import RAISE_PROMPT

WORKER = "raise"
TIMEOUT_S = 120.0
# What one answer may hold, before Python keeps only the number asked for.
MAX_RAISED = 20


class RaisedQuestion(BaseModel):
    question: str = Field(max_length=1000)
    grounded_in: list[str] = Field(default_factory=list, max_length=12)
    why: str = Field(default="", max_length=1000)


class RaiseAnswer(BaseModel):
    questions: list[RaisedQuestion] = Field(default_factory=list, max_length=MAX_RAISED)


@dataclass(frozen=True)
class Raised:
    """What one `raise` call gave: the candidates, or None when it gave no answer."""

    candidates: list[Candidate] | None


def raise_text(
    *, bundle_text: str, title: str, aliases: Sequence[str], existing: Sequence[str], count: int
) -> str:
    """The user text of the `raise` worker."""
    names = ", ".join(aliases) if aliases else "none"
    known = "\n".join(f"- {q}" for q in existing) or "- none"
    return (
        f"The page title: {title}\n"
        f"Its other names: {names}\n"
        f"Raise at most {count} new questions.\n\n"
        f"<dossier_questions>\n{known}\n</dossier_questions>\n\n"
        f"<bundle>\n{bundle_text}\n</bundle>"
    )


async def raise_questions(
    *,
    bundle_text: str,
    title: str,
    aliases: Sequence[str],
    existing: Sequence[str],
    count: int,
    model: str | None,
    runner: Runner | None = None,
    usage_out: list[Usage] | None = None,
) -> Raised:
    """Ask the `raise` worker for up to `count` questions."""
    if count <= 0:
        return Raised([])
    answer = await run_worker(
        name=f"research-{WORKER}",
        system_prompt=RAISE_PROMPT,
        user_text=raise_text(
            bundle_text=bundle_text, title=title, aliases=aliases, existing=existing, count=count
        ),
        answer=RaiseAnswer,
        model=model,
        timeout_s=TIMEOUT_S,
        runner=runner,
        usage_out=usage_out,
    )
    if answer is None:
        return Raised(None)
    return Raised(
        [
            Candidate(
                question=" ".join(q.question.split()),
                grounded_in=[p for p in q.grounded_in if isinstance(p, str)],
                why=" ".join(q.why.split()),
            )
            for q in answer.questions[:count]
        ]
    )
