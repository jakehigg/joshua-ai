"""The insight gate: which raised insights are written.

An insight from the `insight` worker passes when all of these are true:

- `claim` has 15 to 200 characters, and `action` is not empty.
- `confidence` is not `low`.
- `we_do` has 1 phrase or more, each 3 to 160 characters, and each occurs
  verbatim in the parts of the bundle that say what we do: the parts that are
  not under `knowledge/` or `insights/`, so a `snapshots/<name>` part counts.
  Case and white space do not count.
- Each path of `we_do_paths` is one of those parts and holds at least one of
  the phrases. An empty `we_do_paths` becomes the parts that hold the phrases.
- The `basis` rule. `knowledge` (the default): `knowledge_says` has 1 path or
  more, each a published knowledge page of this topic (see `knowledge.py`).
  A topic with no published knowledge page rejects it. `ours`: `we_do` has 2
  phrases or more, the two sides of the defect, and `knowledge_says` can be
  empty. Each path it names is still a published knowledge page of this
  topic.
- `valid_from` and `valid_until` are ISO days, and `valid_until` is after
  `valid_from` and not before today. A missing `valid_from` is today, and a
  missing `valid_until` is today plus `jobs.insights.default_valid_days`.
  `valid_until` is at most today plus `jobs.insights.max_valid_days`.
- It is not a duplicate of an insight of this run that passed.

A claim that is the same as an open insight of the topic, by its normalized
text or by a vector at or above `jobs.insights.dup_sim`, refreshes that
insight instead of making a new one. A claim that is the same as a
`dismissed` insight is dropped with the reason `dismissed before`. Each
rejection has a reason, and the report shows it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from joshua_shared import layout
from joshua_shared.log import get_logger

from joshua_core.jobs.insights.knowledge import KnownPage, normalize_page
from joshua_core.jobs.insights.page import InsightFile
from joshua_core.jobs.insights.worker import RaisedInsight
from joshua_core.jobs.research.gate import Embedder, cosine, normalize
from joshua_core.jobs.research.text import Part, collapse, find_phrase, inline

logger = get_logger("jobs.insights.gate")

CLAIM_MIN_CHARS = 15
CLAIM_MAX_CHARS = 200
MIN_PHRASES = 1
# An insight with `basis: ours` names the two sides of the defect.
MIN_OUR_PHRASES = 2
NO_KNOWLEDGE_PAGE = "basis knowledge needs a published knowledge page, and the topic has none"
PHRASE_MIN_CHARS = 3
PHRASE_MAX_CHARS = 160
DISMISSED_BEFORE = "dismissed before"
LOW_CONFIDENCE = "low confidence"
SEVERITY_RANK = {"urgent": 0, "act": 1, "note": 2}
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}

Kind = Literal["new", "refresh", "rejected"]


@dataclass(frozen=True)
class Checked:
    """A raised insight that passed the shape rules, in the form a page takes."""

    claim: str
    why: str
    action: str
    severity: str
    confidence: str
    basis: str
    valid_from: date
    valid_until: date
    we_do: list[str]
    we_do_paths: list[str]
    knowledge_says: list[str]
    evidence: str
    phrase_paths: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    """The gate's answer for one raised insight. `target` is the open insight
    that a `refresh` refreshes."""

    kind: Kind
    claim: str
    reason: str = ""
    checked: Checked | None = None
    target: InsightFile | None = None


def we_do_parts(parts: Sequence[Part]) -> list[Part]:
    """The parts of a bundle that say what we do: not a knowledge page, and
    not an insight. A snapshot part (`snapshots/<name>`) is one of them."""
    reserved = (f"{layout.KNOWLEDGE}/", f"{layout.INSIGHTS}/")
    return [p for p in parts if not p.path.startswith(reserved)]


def parse_day(value: str | None, name: str) -> tuple[date | None, str]:
    """`(day, "")` for an ISO day or None, else `(None, reason)`."""
    if value is None or not value.strip():
        return None, ""
    try:
        return date.fromisoformat(value.strip()[:10]), ""
    except ValueError:
        return None, f"{name} is not an ISO day: `{inline(value, 40)}`"


def rank(checked: Checked) -> tuple[int, int]:
    """The order in which new insights are kept: severity, then confidence."""
    return (SEVERITY_RANK.get(checked.severity, 9), CONFIDENCE_RANK.get(checked.confidence, 9))


class InsightGate:
    """The gate of one run for one topic. It keeps the vectors it made, so
    each claim is embedded once."""

    def __init__(
        self,
        *,
        parts: Sequence[Part],
        known: Mapping[str, KnownPage],
        open_insights: Sequence[InsightFile],
        dismissed: Sequence[InsightFile],
        today: date,
        default_valid_days: int,
        dup_sim: float,
        embed: Embedder,
        max_valid_days: int | None = None,
    ) -> None:
        self._parts = list(parts)
        self._paths = {p.path: p for p in self._parts}
        self._known = dict(known)
        self._open = list(open_insights)
        self._dismissed = list(dismissed)
        self._today = today
        self._default_days = default_valid_days
        self._max_days = max_valid_days
        self._dup_sim = dup_sim
        self._embed = embed
        self._accepted: list[str] = []
        self._vectors: dict[str, list[list[float]]] = {}
        self._vectors_off = False
        self.notes: list[str] = []

    # -- the shape rules -----------------------------------------------------

    def _we_do(self, raised: RaisedInsight) -> tuple[list[str], list[str], dict[str, str], str]:
        phrases = [" ".join(p.split()) for p in raised.we_do if p.strip()]
        if len(phrases) < MIN_PHRASES:
            return [], [], {}, "we_do has no phrase"
        found: dict[str, str] = {}
        for phrase in phrases:
            if not PHRASE_MIN_CHARS <= len(phrase) <= PHRASE_MAX_CHARS:
                return (
                    [],
                    [],
                    {},
                    f"the phrase `{inline(phrase, 100)}` has {len(phrase)} characters; a "
                    f"phrase has {PHRASE_MIN_CHARS} to {PHRASE_MAX_CHARS}",
                )
            grounding = find_phrase(phrase, self._parts)
            if grounding is None:
                return [], [], {}, f"the phrase `{inline(phrase, 100)}` is not in the bundle"
            found[phrase] = grounding.path
        paths = list(dict.fromkeys(p.strip() for p in raised.we_do_paths if p.strip()))
        if not paths:
            return phrases, list(dict.fromkeys(found.values())), found, ""
        for path in paths:
            part = self._paths.get(path)
            if part is None:
                return [], [], {}, f"we_do_paths names `{inline(path, 120)}`, not a bundle path"
            text = collapse(part.text)
            if not any(collapse(p) in text for p in phrases):
                return [], [], {}, f"`{inline(path, 120)}` holds none of the we_do phrases"
        # A phrase is shown with a named path that holds it, when one does.
        for phrase in phrases:
            for path in paths:
                if collapse(phrase) in collapse(self._paths[path].text):
                    found[phrase] = path
                    break
        return phrases, paths, found, ""

    def _knowledge(self, raised: RaisedInsight, phrases: Sequence[str]) -> tuple[list[str], str]:
        pages = list(dict.fromkeys(normalize_page(p) for p in raised.knowledge_says if p.strip()))
        if raised.basis == "ours":
            if len(phrases) < MIN_OUR_PHRASES:
                return [], (
                    f"basis ours needs {MIN_OUR_PHRASES} we_do phrases or more, the two "
                    "sides of the defect"
                )
        elif not self._known:
            return [], NO_KNOWLEDGE_PAGE
        elif not pages:
            return [], "knowledge_says has no page"
        for page in pages:
            if page not in self._known:
                return [], (
                    f"knowledge_says names `{inline(page, 120)}`, not a published "
                    "knowledge page of this topic"
                )
        return pages, ""

    def _days(self, raised: RaisedInsight) -> tuple[date, date, str]:
        start, why = parse_day(raised.valid_from, "valid_from")
        if why:
            return self._today, self._today, why
        end, why = parse_day(raised.valid_until, "valid_until")
        if why:
            return self._today, self._today, why
        start = start or self._today
        end = end or self._today + timedelta(days=self._default_days)
        if self._max_days is not None:
            end = min(end, self._today + timedelta(days=self._max_days))
        if end <= start:
            return start, end, "valid_until is not after valid_from"
        if end < self._today:
            return start, end, "valid_until is before today"
        return start, end, ""

    def shape(self, raised: RaisedInsight) -> tuple[Checked | None, str]:
        """The checked form of a raised insight, or `(None, reason)`."""
        claim = " ".join(raised.claim.split())
        if not CLAIM_MIN_CHARS <= len(claim) <= CLAIM_MAX_CHARS:
            return None, (
                f"the claim has {len(claim)} characters; it must have "
                f"{CLAIM_MIN_CHARS} to {CLAIM_MAX_CHARS}"
            )
        if not raised.action.strip():
            return None, "the action is empty"
        if raised.confidence == "low":
            return None, LOW_CONFIDENCE
        phrases, paths, found, why = self._we_do(raised)
        if why:
            return None, why
        pages, why = self._knowledge(raised, phrases)
        if why:
            return None, why
        start, end, why = self._days(raised)
        if why:
            return None, why
        return (
            Checked(
                claim=claim,
                why=raised.why.strip(),
                action=raised.action.strip(),
                severity=raised.severity,
                confidence=raised.confidence,
                basis=raised.basis,
                valid_from=start,
                valid_until=end,
                we_do=phrases,
                we_do_paths=paths,
                knowledge_says=pages,
                evidence=raised.evidence.strip(),
                phrase_paths=found,
            ),
            "",
        )

    # -- duplicates ----------------------------------------------------------

    async def _vectors_of(self, text: str) -> list[list[float]] | None:
        if self._vectors_off:
            return None
        if text not in self._vectors:
            try:
                self._vectors[text] = await self._embed(text)
            except RuntimeError as exc:
                self._vectors_off = True
                self.notes.append(
                    "The vector check for duplicates did not run: no embedding model."
                )
                logger.warning({"message": "insight gate has no vectors", "error": str(exc)})
                return None
        return self._vectors[text]

    async def _close(self, claim: str, others: Sequence[str]) -> int | None:
        """The index of the first of `others` that is the same claim, or None."""
        mine = normalize(claim)
        for index, other in enumerate(others):
            if normalize(other) == mine:
                return index
        vectors = await self._vectors_of(claim)
        if not vectors:
            return None
        for index, other in enumerate(others):
            theirs = await self._vectors_of(other)
            if theirs and max(cosine(a, b) for a in vectors for b in theirs) >= self._dup_sim:
                return index
        return None

    async def check(self, raised: RaisedInsight) -> Verdict:
        """Check one raised insight."""
        checked, why = self.shape(raised)
        claim = " ".join(raised.claim.split())
        if checked is None:
            return Verdict("rejected", claim, why)
        hit = await self._close(checked.claim, [f.insight.claim for f in self._open])
        if hit is not None:
            return Verdict("refresh", checked.claim, checked=checked, target=self._open[hit])
        if await self._close(checked.claim, [f.insight.claim for f in self._dismissed]) is not None:
            return Verdict("rejected", checked.claim, DISMISSED_BEFORE)
        if await self._close(checked.claim, self._accepted) is not None:
            return Verdict("rejected", checked.claim, "the same as another insight of this run")
        self._accepted.append(checked.claim)
        return Verdict("new", checked.claim, checked=checked)


def cap(verdicts: Sequence[Verdict], max_new: int) -> tuple[list[Verdict], list[Verdict]]:
    """The new insights to write, at most `max_new`, highest severity first and
    then highest confidence, and the new insights over the cap."""
    new = [v for v in verdicts if v.kind == "new" and v.checked is not None]
    ordered = sorted(new, key=lambda v: rank(v.checked))  # type: ignore[arg-type]
    return ordered[:max_new], ordered[max_new:]
