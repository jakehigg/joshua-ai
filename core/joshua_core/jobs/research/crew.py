"""The crew of one question: the researcher, the challenger, and the editor.

1. The **researcher** is the internet agent with the question and
   `RESEARCH_BRIEF`. It gets the question and nothing else from the wiki.
2. The **challenger** is the internet agent with `CHALLENGE_BRIEF`: the
   question and the draft answer, turned over.
3. The **editor** is a tool-less worker. It gets the question, the local
   context (each `grounded_in` phrase with its sentence of the bundle), the
   draft and the challenge with their sources, and the trust rubric. It
   writes the page.

Python then checks the editor's answer. Each of these is a hold, with a reason:

- a source URL that neither lookup returned is dropped, and the count is
  checked again without it;
- fewer than two sources that are not `weak`, from two registrable domains;
- `verdict: hold`;
- `confidence: low`.

A source on `jobs.research.distrusted_domains` is always `weak`, and a source on
`jobs.research.trusted_domains` is never `weak`. A worker that gives no answer
at any step is a hold with the reason `no answer`. A lookup that timed out is
a hold that says so, with the searches and the fetches it made.

When the caller gives a `draft` and a `challenge` that an earlier run kept,
the crew does not look up again: the editor gets those two.

The two lookups take `jobs.research.lookup_timeout_seconds` and
`jobs.research.lookup_max_turns` in place of the limits of the `internet`
section.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from joshua_shared.config import Internet as InternetConfig
from joshua_shared.config import JobsResearch
from pydantic import BaseModel, Field, field_validator

from joshua_core.engine import internet
from joshua_core.engine.ephemeral import Runner, run_worker
from joshua_core.engine.types import WorkerStats
from joshua_core.engine.url_grants import canonical
from joshua_core.engine.usage import Usage, UsageLedger
from joshua_core.jobs.research.prompts import (
    CHALLENGE_BRIEF,
    EDITOR_PROMPT,
    RESEARCH_BRIEF,
    TRUST_RUBRIC,
)
from joshua_core.jobs.research.text import Grounding, inline

EDITOR = "editor"
INTERNET = internet.WORKER
EDITOR_TIMEOUT_S = 120.0
NO_ANSWER = "no answer"
# `internet.ask` keeps this many characters of the question text.
_LOOKUP_CHARS = 2000
# The second-to-last labels that make a registrable domain three labels long
# under a country code, such as `example.co.uk`.
_SECOND_LEVEL = frozenset({"co", "ac", "gov", "edu", "org", "com"})

Trust = Literal["primary", "secondary", "weak"]


ANSWER_SHORT_CHARS = 160


class EditorSource(BaseModel):
    url: str = Field(max_length=2000)
    title: str = Field(default="", max_length=300)
    trust: Trust


class EditorAnswer(BaseModel):
    """The answer of the editor. The model gets `EDITOR_SCHEMA`, and this model
    checks the limits after it. A longer `answer_short` is cut, because the page
    keeps only its first line."""

    answer: str = Field(max_length=20000)
    answer_short: str = Field(max_length=ANSWER_SHORT_CHARS)
    local_notes: str = Field(default="", max_length=6000)
    evidence: list[str] = Field(default_factory=list, max_length=30)
    disagreements: str = Field(default="", max_length=6000)
    sources: list[EditorSource] = Field(default_factory=list, max_length=20)
    confidence: Literal["low", "medium", "high"]
    verdict: Literal["publish", "hold"]
    reason: str = Field(default="", max_length=1000)

    @field_validator("answer_short", mode="before")
    @classmethod
    def _cut_short(cls, value: object) -> object:
        if isinstance(value, str) and len(value) > ANSWER_SHORT_CHARS:
            return value[: ANSWER_SHORT_CHARS - 1].rstrip() + "…"
        return value


def _string() -> dict[str, str]:
    return {"type": "string"}


def _enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


# The schema the editor is asked for. It is flat: no reference, no length
# limit, and an enum only for `trust`, `confidence`, and `verdict`. Each try
# that the validator of the CLI refuses costs a turn, so a simple schema leaves
# the turns for the answer. `EditorAnswer` checks the limits after it.
EDITOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": _string(),
        "answer_short": _string(),
        "local_notes": _string(),
        "evidence": {"type": "array", "items": _string()},
        "disagreements": _string(),
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": _string(),
                    "title": _string(),
                    "trust": _enum("primary", "secondary", "weak"),
                },
                "required": ["url", "trust"],
            },
        },
        "confidence": _enum("low", "medium", "high"),
        "verdict": _enum("publish", "hold"),
        "reason": _string(),
    },
    "required": ["answer", "answer_short", "sources", "confidence", "verdict"],
}


# -- domains -------------------------------------------------------------------


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _bare(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def domain_matches(host: str, entry: str) -> bool:
    """True when a domain list entry covers `host`.

    The entry matches when the host equals it, when the host ends with it (an
    entry like `.edu`, or a name like `reddit.com` for `www.reddit.com`), or,
    for an entry that ends with a dot (`extension.`), when the host starts
    with it.
    """
    host = host.lower().rstrip(".")
    entry = entry.strip().lower()
    if not host or not entry:
        return False
    if entry.endswith("."):
        return host.startswith(entry) or _bare(host).startswith(entry)
    if host == entry:
        return True
    if entry.startswith("."):
        return host.endswith(entry)
    return host.endswith("." + entry)


def on_list(url: str, entries: Sequence[str]) -> bool:
    host = host_of(url)
    return any(domain_matches(host, entry) for entry in entries)


def registrable_domain(url_or_host: str) -> str:
    """The registrable domain of a URL or a host: the host without a leading
    `www.`, cut to its last two labels, or its last three when the label
    before a two-letter country code is `co`, `ac`, `gov`, `edu`, `org`, or
    `com` (`example.co.uk`)."""
    host = host_of(url_or_host) if "://" in url_or_host else url_or_host.lower().rstrip(".")
    labels = [label for label in _bare(host).split(".") if label]
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# -- the result ------------------------------------------------------------------


@dataclass
class CrewResult:
    """What the crew gave for one question.

    `page` is the editor's answer after the checks, or None when a step gave
    no answer. `holds` lists the reasons to hold; it is empty for a page that
    may be published. `dropped_urls` are the sources the editor named that no
    lookup returned.
    """

    page: EditorAnswer | None
    holds: list[str] = field(default_factory=list)
    dropped_urls: list[str] = field(default_factory=list)
    draft: internet.InternetAnswer | None = None
    challenge: internet.InternetAnswer | None = None

    @property
    def publish(self) -> bool:
        return self.page is not None and not self.holds

    @property
    def hold_reason(self) -> str:
        return "; ".join(self.holds)


def check_editor(
    answer: EditorAnswer,
    given: Sequence[str],
    settings: JobsResearch,
) -> CrewResult:
    """Apply the Python checks to an editor answer. `given` are the source URLs
    that the two lookups returned."""
    allowed = {canonical(u) for u in given if canonical(u)}
    kept: list[EditorSource] = []
    dropped: list[str] = []
    for source in answer.sources:
        key = canonical(source.url)
        if not key or key not in allowed or any(canonical(s.url) == key for s in kept):
            if not key or key not in allowed:
                dropped.append(source.url)
            continue
        if on_list(source.url, settings.distrusted_domains):
            source.trust = "weak"
        elif on_list(source.url, settings.trusted_domains) and source.trust == "weak":
            source.trust = "secondary"
        kept.append(source)
    answer.sources = kept
    holds: list[str] = []
    domains = {registrable_domain(s.url) for s in kept if s.trust != "weak"}
    domains.discard("")
    if len(domains) < 2:
        holds.append(
            f"{len(domains)} independent sources that are not weak; two from two domains are needed"
        )
    if answer.verdict == "hold":
        holds.append(f"the editor held it: {inline(answer.reason, 300) or 'no reason given'}")
    if answer.confidence == "low":
        holds.append("the confidence is low")
    return CrewResult(page=answer, holds=holds, dropped_urls=dropped)


# -- the steps -------------------------------------------------------------------


def no_answer_reason(who: str, stats: WorkerStats) -> str:
    """The hold reason for a lookup that gave no answer. A lookup that timed
    out says so, with its tool counts."""
    if not stats.timed_out:
        return f"{NO_ANSWER} from {who}"
    seconds = f"{stats.timeout_s:g}" if stats.timeout_s is not None else "?"
    counts = f"{stats.searches} searches, {stats.fetches} fetches"
    if who == "the researcher":
        return f"lookup timed out after {seconds} s ({counts})"
    return f"the lookup of {who} timed out after {seconds} s ({counts})"


def research_text(question: str) -> str:
    return question + RESEARCH_BRIEF


def challenge_text(question: str, draft: str) -> str:
    """The challenge brief with the draft cut so that the whole text fits the
    question limit of the internet agent."""
    empty = CHALLENGE_BRIEF.format(question=question, draft="")
    room = max(0, _LOOKUP_CHARS - len(empty) - 1)
    cut = draft if len(draft) <= room else draft[: max(0, room - 1)] + "…"
    return CHALLENGE_BRIEF.format(question=question, draft=cut)


def _sources_block(answer: internet.InternetAnswer) -> str:
    lines = [f"- {s.url} ({inline(s.title, 200) or 'no title'})" for s in answer.sources]
    return "\n".join(lines) or "- none"


def editor_text(
    question: str,
    groundings: Sequence[Grounding],
    draft: internet.InternetAnswer,
    challenge: internet.InternetAnswer,
    settings: JobsResearch,
) -> str:
    """The user text of the editor."""
    local = "\n".join(f'- "{g.phrase}" ({g.path}): {g.sentence}' for g in groundings) or "- none"
    trusted = ", ".join(settings.trusted_domains) or "none"
    distrusted = ", ".join(settings.distrusted_domains) or "none"
    return (
        f"<question>\n{question}\n</question>\n\n"
        f"<local_context>\n{local}\n</local_context>\n\n"
        f'<draft confidence="{draft.confidence}">\n{draft.answer}\n\n'
        f"Disagreements: {draft.disagreements or 'none'}\n\nSources:\n{_sources_block(draft)}\n"
        f"</draft>\n\n"
        f'<challenge confidence="{challenge.confidence}">\n{challenge.answer}\n\n'
        f"Disagreements: {challenge.disagreements or 'none'}\n\n"
        f"Sources:\n{_sources_block(challenge)}\n</challenge>\n\n"
        f"<trust_rubric>\n{TRUST_RUBRIC}\n\nTrusted domains: {trusted}\n"
        f"Distrusted domains: {distrusted}\n</trust_rubric>"
    )


async def research_question(
    question: str,
    groundings: Sequence[Grounding],
    *,
    internet_settings: InternetConfig,
    research_settings: JobsResearch,
    editor_model: str | None,
    ledger: UsageLedger,
    internet_runner: Runner | None = None,
    editor_runner: Runner | None = None,
    draft: internet.InternetAnswer | None = None,
    challenge: internet.InternetAnswer | None = None,
) -> CrewResult:
    """Run the crew for one question. Each call is counted on `ledger`.

    When both `draft` and `challenge` are given, the two lookups do not run,
    and the editor gets these two answers.
    """

    async def lookup(text: str, stats: WorkerStats) -> internet.InternetAnswer | None:
        used: list[Usage] = []
        got = await internet.ask(
            text,
            settings=internet_settings,
            runner=internet_runner,
            usage_out=used,
            timeout_s=research_settings.lookup_timeout_seconds,
            max_turns=research_settings.lookup_max_turns,
            stats_out=stats,
        )
        ledger.add(INTERNET, used)
        return got

    if draft is None or challenge is None:
        stats = WorkerStats()
        draft = await lookup(research_text(question), stats)
        if draft is None:
            return CrewResult(page=None, holds=[no_answer_reason("the researcher", stats)])
        stats = WorkerStats()
        challenge = await lookup(challenge_text(question, draft.answer), stats)
        if challenge is None:
            return CrewResult(
                page=None, holds=[no_answer_reason("the challenger", stats)], draft=draft
            )

    used: list[Usage] = []
    edited = await run_worker(
        name=f"research-{EDITOR}",
        system_prompt=EDITOR_PROMPT,
        user_text=editor_text(question, groundings, draft, challenge, research_settings),
        answer=EditorAnswer,
        schema=EDITOR_SCHEMA,
        model=editor_model,
        timeout_s=EDITOR_TIMEOUT_S,
        runner=editor_runner,
        usage_out=used,
    )
    ledger.add(EDITOR, used)
    if edited is None:
        return CrewResult(
            page=None, holds=[f"{NO_ANSWER} from the editor"], draft=draft, challenge=challenge
        )
    given = [s.url for s in draft.sources] + [s.url for s in challenge.sources]
    result = check_editor(edited, given, research_settings)
    result.draft = draft
    result.challenge = challenge
    return result
