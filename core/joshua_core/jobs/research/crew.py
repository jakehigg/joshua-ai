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
section. The editor takes `jobs.research.editor_timeout_seconds`.
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
RESEARCHER = "researcher"
CHALLENGER = "challenger"
STEPS = (RESEARCHER, CHALLENGER, EDITOR)
INTERNET = internet.WORKER
NO_ANSWER = "no answer"
# `internet.ask` keeps this many characters of the question text.
_LOOKUP_CHARS = 2000
# The second-to-last labels that make a registrable domain three labels long
# under a country code, such as `example.co.uk`.
_SECOND_LEVEL = frozenset({"co", "ac", "gov", "edu", "org", "com"})

Trust = Literal["primary", "secondary", "weak"]


ANSWER_SHORT_CHARS = 160
# The limits of the editor's answer. The schema states each one as guidance,
# and `EditorAnswer` cuts a longer value to it.
ANSWER_CHARS = 2500
LOCAL_NOTES_CHARS = 800
EVIDENCE_ITEMS = 8
EVIDENCE_CHARS = 300
DISAGREEMENTS_CHARS = 800
REASON_CHARS = 300
SOURCE_ITEMS = 8
TITLE_CHARS = 200
URL_CHARS = 500
_MORE = "…"


def cut_text(value: object, limit: int) -> object:
    """A string longer than `limit` cut to `limit` characters, the last one
    `…`. Any other value is returned as it is, for the model to check."""
    if isinstance(value, str) and len(value) > limit:
        return value[: limit - 1].rstrip() + _MORE
    return value


def cut_list(value: object, items: int, chars: int | None = None) -> object:
    """A list cut to `items` items, and each string in it to `chars`
    characters. Any other value is returned as it is."""
    if not isinstance(value, list):
        return value
    kept = value[:items]
    return kept if chars is None else [cut_text(item, chars) for item in kept]


class EditorSource(BaseModel):
    url: str = Field(max_length=URL_CHARS)
    title: str = Field(default="", max_length=TITLE_CHARS)
    trust: Trust

    @field_validator("url", mode="before")
    @classmethod
    def _cut_url(cls, value: object) -> object:
        return cut_text(value, URL_CHARS)

    @field_validator("title", mode="before")
    @classmethod
    def _cut_title(cls, value: object) -> object:
        return cut_text(value, TITLE_CHARS)


class EditorAnswer(BaseModel):
    """The answer of the editor, in the order of `EDITOR_SCHEMA`: the short
    required fields first, then the long ones.

    The model gets `EDITOR_SCHEMA`, and this model checks the answer after it.
    A string or a list that is longer than its limit is cut to the limit, not
    refused, so a long field never loses the whole answer."""

    verdict: Literal["publish", "hold"]
    confidence: Literal["low", "medium", "high"]
    answer_short: str = Field(max_length=ANSWER_SHORT_CHARS)
    sources: list[EditorSource] = Field(default_factory=list, max_length=SOURCE_ITEMS)
    answer: str = Field(max_length=ANSWER_CHARS)
    local_notes: str = Field(default="", max_length=LOCAL_NOTES_CHARS)
    evidence: list[str] = Field(default_factory=list, max_length=EVIDENCE_ITEMS)
    disagreements: str = Field(default="", max_length=DISAGREEMENTS_CHARS)
    reason: str = Field(default="", max_length=REASON_CHARS)

    @field_validator("answer_short", mode="before")
    @classmethod
    def _cut_short(cls, value: object) -> object:
        return cut_text(value, ANSWER_SHORT_CHARS)

    @field_validator("answer", mode="before")
    @classmethod
    def _cut_answer(cls, value: object) -> object:
        return cut_text(value, ANSWER_CHARS)

    @field_validator("local_notes", mode="before")
    @classmethod
    def _cut_local_notes(cls, value: object) -> object:
        return cut_text(value, LOCAL_NOTES_CHARS)

    @field_validator("disagreements", mode="before")
    @classmethod
    def _cut_disagreements(cls, value: object) -> object:
        return cut_text(value, DISAGREEMENTS_CHARS)

    @field_validator("reason", mode="before")
    @classmethod
    def _cut_reason(cls, value: object) -> object:
        return cut_text(value, REASON_CHARS)

    @field_validator("evidence", mode="before")
    @classmethod
    def _cut_evidence(cls, value: object) -> object:
        return cut_list(value, EVIDENCE_ITEMS, EVIDENCE_CHARS)

    @field_validator("sources", mode="before")
    @classmethod
    def _cut_sources(cls, value: object) -> object:
        return cut_list(value, SOURCE_ITEMS)


def _string(limit: int) -> dict[str, str]:
    return {"type": "string", "description": f"At most {limit} characters."}


def _enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


def _array(items: dict[str, Any], limit: int) -> dict[str, Any]:
    return {"type": "array", "items": items, "description": f"At most {limit} items."}


# The schema the editor is asked for. It is flat: no reference, and an enum only
# for `trust`, `confidence`, and `verdict`. The short required fields come
# first, so that an answer that is cut off still has them. Each limit is a
# `description`, not a `maxLength` or a `maxItems`: the validator of the CLI
# refuses a value over a hard limit, and each refused try costs a turn.
# `EditorAnswer` cuts a longer value to its limit.
EDITOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": _enum("publish", "hold"),
        "confidence": _enum("low", "medium", "high"),
        "answer_short": _string(ANSWER_SHORT_CHARS),
        "sources": _array(
            {
                "type": "object",
                "properties": {
                    "url": _string(URL_CHARS),
                    "title": _string(TITLE_CHARS),
                    "trust": _enum("primary", "secondary", "weak"),
                },
                "required": ["url", "trust"],
            },
            SOURCE_ITEMS,
        ),
        "answer": _string(ANSWER_CHARS),
        "local_notes": _string(LOCAL_NOTES_CHARS),
        "evidence": _array(_string(EVIDENCE_CHARS), EVIDENCE_ITEMS),
        "disagreements": _string(DISAGREEMENTS_CHARS),
        "reason": _string(REASON_CHARS),
    },
    "required": ["verdict", "confidence", "answer_short", "sources", "answer"],
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
    lookup returned. `usage` holds the tokens of each step that ran, by step
    name: `researcher`, `challenger`, and `editor`.
    """

    page: EditorAnswer | None
    holds: list[str] = field(default_factory=list)
    dropped_urls: list[str] = field(default_factory=list)
    draft: internet.InternetAnswer | None = None
    challenge: internet.InternetAnswer | None = None
    usage: dict[str, Usage] = field(default_factory=dict)

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
    """Run the crew for one question. Each call is counted on `ledger`, and
    the tokens of each step are on `CrewResult.usage`.

    When both `draft` and `challenge` are given, the two lookups do not run,
    and the editor gets these two answers.
    """
    steps: dict[str, Usage] = {}

    async def lookup(step: str, text: str, stats: WorkerStats) -> internet.InternetAnswer | None:
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
        steps[step] = Usage.sum(used)
        return got

    if draft is None or challenge is None:
        stats = WorkerStats()
        draft = await lookup(RESEARCHER, research_text(question), stats)
        if draft is None:
            return CrewResult(
                page=None, holds=[no_answer_reason("the researcher", stats)], usage=steps
            )
        stats = WorkerStats()
        challenge = await lookup(CHALLENGER, challenge_text(question, draft.answer), stats)
        if challenge is None:
            return CrewResult(
                page=None,
                holds=[no_answer_reason("the challenger", stats)],
                draft=draft,
                usage=steps,
            )

    used: list[Usage] = []
    edited = await run_worker(
        name=f"research-{EDITOR}",
        system_prompt=EDITOR_PROMPT,
        user_text=editor_text(question, groundings, draft, challenge, research_settings),
        answer=EditorAnswer,
        schema=EDITOR_SCHEMA,
        model=editor_model,
        timeout_s=research_settings.editor_timeout_seconds,
        runner=editor_runner,
        usage_out=used,
    )
    ledger.add(EDITOR, used)
    steps[EDITOR] = Usage.sum(used)
    if edited is None:
        return CrewResult(
            page=None,
            holds=[f"{NO_ANSWER} from the editor"],
            draft=draft,
            challenge=challenge,
            usage=steps,
        )
    given = [s.url for s in draft.sources] + [s.url for s in challenge.sources]
    result = check_editor(edited, given, research_settings)
    result.draft = draft
    result.challenge = challenge
    result.usage = steps
    return result
