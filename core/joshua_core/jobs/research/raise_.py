"""The `raise` worker: questions about one topic, each for one audience.

A `web` question is a pair. The general question (`general_question`) goes to
the internet agent, which searches the open web and has none of our pages. It
names the class of the thing and its public specifics only: the species, the
product model, the technique, the material, the season, the condition of the
thing. `local_context` holds our specifics and why we ask, and it stays inside
the instance: the editor reads it, and the lookups never do. `subject` is the
generic name of the thing, when the general question holds no name of the
topic. The worker sets `about_person` on each question, and the gate rejects
a `web` question with `about_person: true`. A `person` question asks, in its
own `question` text, for a decision that only somebody here can make or tell.
A `record` question asks what our own pages and journal already hold.

The worker reads the memory bundle of the topic, its title and aliases, and
the questions the dossier holds. It has no tools. The gate in `gate.py` then
checks each question and gives its route.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import Field

from joshua_core.engine.ephemeral import Runner, run_worker
from joshua_core.engine.sanitize import CleanModel
from joshua_core.engine.usage import Usage
from joshua_core.jobs.research.gate import PERSON, RECORD, WEB, Candidate
from joshua_core.jobs.research.prompts import RAISE_PROMPT

WORKER = "raise"
TIMEOUT_S = 120.0
# What one answer may hold, before Python keeps only the number asked for.
MAX_RAISED = 20
# The schema limits are wide, so that the gate, not the schema, rejects a
# text that is too long, and gives the reason.
_TEXT_CHARS = 1000
_LOCAL_CHARS = 4000
_SUBJECT_CHARS = 400


class RaisedQuestion(CleanModel):
    """One question as the worker gives it. Each text field is cleaned of
    leaked markup.

    A `web` question has `general_question` and `local_context`. A `person`
    or a `record` question has `question`. When the field of the audience is
    empty, the other text field takes its place."""

    question: str = Field(default="", max_length=_TEXT_CHARS)
    general_question: str = Field(default="", max_length=_TEXT_CHARS)
    local_context: str = Field(default="", max_length=_LOCAL_CHARS)
    subject: str = Field(default="", max_length=_SUBJECT_CHARS)
    grounded_in: list[str] = Field(default_factory=list, max_length=12)
    why: str = Field(default="", max_length=1000)
    # `web`, `person`, or `record`. The gate reads any other value as `web`.
    audience: str = Field(default=WEB, max_length=40)
    # True when the question carries a specific of a person, named or not.
    about_person: bool


class RaiseAnswer(CleanModel):
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
    return Raised([candidate(q) for q in answer.questions[:count]])


def candidate(raised: RaisedQuestion) -> Candidate:
    """The gate's candidate of one raised question. A `web` question takes
    its general question as its text."""
    audience = raised.audience.strip().lower()
    if audience in (PERSON, RECORD):
        text = raised.question or raised.general_question
    else:
        text = raised.general_question or raised.question
    return Candidate(
        question=" ".join(text.split()),
        grounded_in=[p for p in raised.grounded_in if isinstance(p, str)],
        why=" ".join(raised.why.split()),
        audience=audience,
        about_person=raised.about_person,
        local_context=" ".join(raised.local_context.split()),
        subject=" ".join(raised.subject.split()),
    )
