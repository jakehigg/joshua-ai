"""The specificity gate: which raised questions a dossier takes.

A question from the `raise` worker passes when all of these are true:

- `grounded_in` has 2 phrases or more, each 3 to 80 characters, and each
  occurs verbatim in the bundle text. Case and white space do not count. There
  is no stemming and no fuzzy match.
- The question names the topic: it holds a name of the topic by the rule of
  `topics.names_pattern`. The names are the title, the title without a
  parenthetical at its end, each alias, and the first words of the file stem.
- The question is 20 to 300 characters and ends with `?`.
- It is a question about the world, not about us, because the internet agent
  searches the open web with it and has none of our pages:
  - it names no person on the roster, by a display name or an id, as whole
    words in any case;
  - it holds no date in ISO form (`2026-10-04`) or in `Month D` form
    (`October 4`, `Oct 4`);
  - it does not start with `what did`, `why did`, `when did`, `will`,
    `should we`, `did we`, or `do we`, and it holds no `we`, `our`, or `us`
    as a word. `US` in capitals is a country and does not count.
- It is not a duplicate: its normalized text is not the text of a question of
  the dossier, and its vector is below `jobs.research.dup_sim` to each question
  of the dossier and to each `kind: knowledge` page of the index. A duplicate
  of a knowledge page names that page in `answered_by`.

A question that a person asks passes the length rule and the topic rule only.
Each rejection has a reason, and the report shows it.
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from joshua_shared import frontmatter, layout
from joshua_shared.log import get_logger

from joshua_core.jobs.bundle import BundleStore
from joshua_core.jobs.research.text import collapse
from joshua_core.jobs.topics import names_pattern, read_page
from joshua_core.memory.indexer import embed_document

logger = get_logger("jobs.research.gate")

MIN_PHRASES = 2
PHRASE_MIN_CHARS = 3
PHRASE_MAX_CHARS = 80
QUESTION_MIN_CHARS = 20
QUESTION_MAX_CHARS = 300
KNOWLEDGE_KIND = "knowledge"
# The openings of a question about what we did, do, or will do.
US_OPENINGS = ("what did", "why did", "when did", "will", "should we", "did we", "do we")
_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)
_ISO_DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_MONTH_DAY_RE = re.compile(
    rf"(?<!\w)(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?!\w)", re.IGNORECASE
)
_US_WORD_RE = re.compile(r"(?<!\w)(?:(?i:we|our)|us)(?!\w)")
_INDEX_WIKI_PREFIX = "wiki/"

# Embeds one text and gives its vectors. A test passes its own.
Embedder = Callable[[str], Awaitable[list[list[float]]]]


@dataclass(frozen=True)
class Candidate:
    """A question to check, with its phrases and why it matters."""

    question: str
    grounded_in: list[str] = field(default_factory=list)
    why: str = ""


@dataclass(frozen=True)
class Verdict:
    """The gate's answer for one candidate. `reason` is empty when it passed."""

    candidate: Candidate
    accepted: bool
    reason: str = ""
    answered_by: str | None = None


def normalize(text: str) -> str:
    """The text of a question for an exact comparison: one case, one space,
    and no punctuation at the end."""
    return collapse(text).rstrip(" ?.!")


def names_topic(question: str, terms: Sequence[str]) -> bool:
    """True when `question` holds one of `terms` by the rule of
    `topics.names_pattern`. Give it `topics.topic_names` of the topic."""
    pattern = names_pattern(terms)
    return pattern is not None and pattern.search(question) is not None


def roster_names(settings: Any) -> list[str]:
    """The display name and the id of each person in `settings.people`."""
    names: list[str] = []
    for person in getattr(settings, "people", None) or []:
        names.extend([person.name, person.id])
    return [n for n in dict.fromkeys(names) if n.strip()]


def world_reason(question: str, people: Sequence[str] = ()) -> str:
    """Why a question is about us and not about the world, or `""`. See the
    module docstring."""
    text = " ".join(question.split())
    for name in people:
        words = r"\s+".join(re.escape(w) for w in name.split())
        if words and re.search(rf"(?<!\w){words}(?!\w)", text, re.IGNORECASE):
            return "the question names a person; ask about the thing, not about a person"
    if _ISO_DATE_RE.search(text) or _MONTH_DAY_RE.search(text):
        return "the question holds a date from our records; the web does not know it"
    lower = text.casefold()
    for opening in US_OPENINGS:
        if re.match(rf"{re.escape(opening)}(?!\w)", lower):
            return f"the question starts with `{opening}`: it asks about us, not the world"
    if _US_WORD_RE.search(text):
        return "the question asks about us (`we`, `our`, `us`), not about the world"
    return ""


def shape_reason(question: str, terms: Sequence[str]) -> str:
    """Why a question fails the length or the topic rule, or `""`."""
    text = question.strip()
    if not QUESTION_MIN_CHARS <= len(text) <= QUESTION_MAX_CHARS:
        return (
            f"the question has {len(text)} characters; it must have "
            f"{QUESTION_MIN_CHARS} to {QUESTION_MAX_CHARS}"
        )
    if not text.endswith("?"):
        return "the question does not end with a question mark"
    if not names_topic(text, terms):
        return "the question does not name the topic"
    return ""


def grounding_reason(phrases: Sequence[str], bundle_text: str) -> str:
    """Why the `grounded_in` phrases fail, or `""`."""
    if len(phrases) < MIN_PHRASES:
        return f"grounded_in has {len(phrases)} phrases; it must have {MIN_PHRASES} or more"
    haystack = collapse(bundle_text)
    for phrase in phrases:
        size = len(phrase.strip())
        if not PHRASE_MIN_CHARS <= size <= PHRASE_MAX_CHARS:
            return (
                f"the phrase `{phrase[:100]}` has {size} characters; a phrase has "
                f"{PHRASE_MIN_CHARS} to {PHRASE_MAX_CHARS}"
            )
        if collapse(phrase) not in haystack:
            return f"the phrase `{phrase[:100]}` is not in the bundle"
    return ""


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def default_embedder(settings: Any, title: str) -> Embedder:
    """The indexer's own path from a text to its vectors, as the bundle uses it."""

    async def embed(text: str) -> list[list[float]]:
        chunks = await embed_document(
            title,
            text,
            model=settings.memory.embed_model,
            chunk_chars=settings.memory.chunk_chars,
        )
        return [vec for _, _, vec in chunks]

    return embed


def _is_answer_page(index_path: str, data_root: Path) -> str | None:
    """The wiki-relative path of a knowledge page with a `question`, or None."""
    if not index_path.startswith(_INDEX_WIKI_PREFIX):
        return None
    rel = index_path[len(_INDEX_WIKI_PREFIX) :]
    wiki = layout.wiki_root(data_root)
    path = wiki / rel
    if layout.is_hidden(path, wiki) or not rel.startswith(f"{layout.KNOWLEDGE}/"):
        return None
    meta, _ = frontmatter.split(read_page(path))
    return rel if isinstance(meta.get("question"), str) else None


class Gate:
    """The gate of one run for one dossier. It keeps the vectors it made, so
    each question is embedded once."""

    def __init__(
        self,
        *,
        bundle_text: str,
        terms: Sequence[str],
        existing: Sequence[str],
        store: BundleStore,
        dup_sim: float,
        data_root: Path,
        embed: Embedder,
        people: Sequence[str] = (),
    ) -> None:
        self._bundle_text = bundle_text
        self._terms = [t for t in terms if t.strip()]
        self._people = [p for p in people if p.strip()]
        self._texts: list[str] = list(existing)
        self._store = store
        self._dup_sim = dup_sim
        self._data_root = Path(data_root)
        self._embed = embed
        self._vectors: dict[str, list[list[float]]] = {}
        self.notes: list[str] = []
        self._vectors_off = False

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
                logger.warning({"message": "research gate has no vectors", "error": str(exc)})
                return None
        return self._vectors[text]

    async def _duplicate(self, question: str) -> tuple[str, str | None]:
        """Why `question` is a duplicate and of which page, or `("", None)`."""
        mine = normalize(question)
        for known in self._texts:
            if normalize(known) == mine:
                return "the same question is in the dossier", None
        vectors = await self._vectors_of(question)
        if not vectors:
            return "", None
        for known in self._texts:
            others = await self._vectors_of(known)
            if not others:
                continue
            best = max(cosine(a, b) for a in vectors for b in others)
            if best >= self._dup_sim:
                return (
                    f"a question of the dossier is this close ({best:.2f}): `{known[:120]}`",
                    None,
                )
        try:
            near = await self._store.kb_near_paths(
                vectors, kinds=[KNOWLEDGE_KIND], min_sim=self._dup_sim
            )
        except Exception as exc:  # noqa: BLE001 — no index means no index check
            self.notes.append("The index check for duplicates did not run.")
            logger.warning({"message": "research gate index failed", "error": str(exc)})
            return "", None
        for index_path, sim in sorted(near.items(), key=lambda item: (-item[1], item[0])):
            page = _is_answer_page(index_path, self._data_root)
            if page is not None:
                return f"a knowledge page is this close ({sim:.2f})", page
        return "", None

    async def check(self, candidate: Candidate, *, person: bool = False) -> Verdict:
        """Check one candidate. An accepted one counts as known for the next."""
        reason = shape_reason(candidate.question, self._terms)
        if not reason and not person:
            reason = world_reason(candidate.question, self._people)
        if not reason and not person:
            reason = grounding_reason(candidate.grounded_in, self._bundle_text)
        if reason:
            return Verdict(candidate, False, reason)
        if not person:
            why, page = await self._duplicate(candidate.question)
            if why:
                return Verdict(candidate, False, why, answered_by=page)
        self._texts.append(candidate.question)
        return Verdict(candidate, True)
