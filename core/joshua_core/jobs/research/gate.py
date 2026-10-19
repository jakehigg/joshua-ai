"""The specificity gate and the route: which raised questions a dossier
takes, which go to a person, and which our records answer.

A question from the `raise` worker has an audience: `web`, `person`, or
`record`. Each question, for each audience, must pass these rules, or it is
rejected:

- `grounded_in` has 2 phrases or more, each 3 to 160 characters, and each
  occurs verbatim in the bundle text. Case and white space do not count. There
  is no stemming and no fuzzy match.
- The question names the topic: it holds a name of the topic by the rule of
  `topics.names_pattern`. The names are the title, the title without a
  parenthetical at its end, each alias, and the first words of the file stem.
- The question is 20 to 300 characters and ends with `?`.
- It is not a duplicate: its normalized text is not the text of a question of
  the dossier, and its vector is below `jobs.research.dup_sim` to each question
  of the dossier and to each `kind: knowledge` page of the index. A duplicate
  of a knowledge page names that page in `answered_by`.

Then the route. The internet agent searches the open web and has none of our
pages, so a question about us goes to `person`, whatever the worker said, when
one of these is true (`world_rule` names the rule in `route_reason`):

- it names a person on the roster, by a display name or an id, as whole
  words in any case (`names a person`);
- it holds a date in ISO form (`2026-10-04`) or in `Month D` form
  (`October 4`, `Oct 4`) (`names a record date`);
- it starts with `what did`, `why did`, `when did`, `will`, `should we`,
  `did we`, or `do we` (`asks what we did or will do`);
- it holds `we`, `our`, or `us` as a word (`asks about us`). `US` in capitals
  is a country and does not count.

The `raise` worker also judges each question: `about_person: true` says that
it carries a specific of a person, named or not, such as a condition, a
symptom, a diet, a body measurement, a mood, a finance, a location, or a
relationship. Such a `web` or `person` question goes to
`person` with `route_reason: about a person`, and a `record` one still goes to
the record search, which stays inside the instance. The worker judges, and
Python enforces: the rules above still apply when the worker says `false`.

A topic in a person section (`jobs.research.person_sections`) is about
people, so its questions go to `person` too, whatever the worker said, with
`route_reason: person section`. A `record` question of such a topic still
goes to the record search, because the search stays inside the instance.

A `web` question goes to the dossier. A `record` question goes to the memory
search over the `wiki`, `journal`, and `knowledge` kinds: a hit at
`jobs.research.record_sim` or more closes it with `answered_by: <path>`, and
with no such hit it goes to `person` (`no record found`). A `person` question
from the worker needs `why`. A `person` question whose text, or whose vector
at `dup_sim` or more, matches an open or answered question page of the topic
is `already asked`; a match on a dismissed one is `dismissed before`.

A question that a person asks passes the length rule and the topic rule only,
and it goes to the dossier. Each rejection has a reason, and the report shows
it.
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

from joshua_core.jobs.bundle import BundleStore, is_job_entry
from joshua_core.jobs.research.text import collapse
from joshua_core.jobs.topics import RESERVED_FOLDERS, names_pattern, read_page
from joshua_core.memory.indexer import embed_document

logger = get_logger("jobs.research.gate")

MIN_PHRASES = 2
PHRASE_MIN_CHARS = 3
PHRASE_MAX_CHARS = 160
QUESTION_MIN_CHARS = 20
QUESTION_MAX_CHARS = 300
KNOWLEDGE_KIND = "knowledge"
# The kinds of the index that hold our records.
RECORD_KINDS = ("wiki", "journal", "knowledge")
# The folders of the wiki where a record can be: the topic pages, the journal,
# and the knowledge pages.
_RECORD_FOLDERS = ("journal", layout.KNOWLEDGE)

WEB = "web"
PERSON = "person"
RECORD = "record"
AUDIENCES = (WEB, PERSON, RECORD)

RULE_PERSON = "names a person"
RULE_DATE = "names a record date"
RULE_OPENING = "asks what we did or will do"
RULE_US = "asks about us"
ROUTE_WORKER = "the raise worker asks a person"
PERSON_SECTION = "person section"
ABOUT_PERSON = "about a person"
NO_RECORD = "no record found"
ALREADY_ASKED = "already asked"
DISMISSED_BEFORE = "dismissed before"
DEFAULT_RECORD_SIM = 0.80
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
    """A question to check, with its phrases, why it matters, its audience, and
    whether the worker says it carries a specific of a person."""

    question: str
    grounded_in: list[str] = field(default_factory=list)
    why: str = ""
    audience: str = WEB
    about_person: bool = False


@dataclass(frozen=True)
class Verdict:
    """The gate's answer for one candidate. `reason` is empty when it passed.

    `route` is `web`, `person`, or `record` for a candidate that passed the
    rules, and `route_reason` says why a question went to `person`. A
    `record` question names its record in `answered_by`. A `person` question
    that is not accepted, because it was asked before, names that question
    page in `page`.
    """

    candidate: Candidate
    accepted: bool
    reason: str = ""
    answered_by: str | None = None
    route: str = ""
    route_reason: str = ""
    page: str | None = None


@dataclass(frozen=True)
class AskedQuestion:
    """A question page of the topic: its text, its status, and its path
    relative to the wiki root."""

    question: str
    status: str
    path: str | None = None


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


def world_rule(question: str, people: Sequence[str] = ()) -> str:
    """The short name of the rule that makes a question about us, or `""`.
    See the module docstring."""
    text = " ".join(question.split())
    for name in people:
        words = r"\s+".join(re.escape(w) for w in name.split())
        if words and re.search(rf"(?<!\w){words}(?!\w)", text, re.IGNORECASE):
            return RULE_PERSON
    if _ISO_DATE_RE.search(text) or _MONTH_DAY_RE.search(text):
        return RULE_DATE
    lower = text.casefold()
    for opening in US_OPENINGS:
        if re.match(rf"{re.escape(opening)}(?!\w)", lower):
            return RULE_OPENING
    if _US_WORD_RE.search(text):
        return RULE_US
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


def _record_page(index_path: str, data_root: Path) -> str | None:
    """The wiki-relative path of an index hit that is one of our records, or
    None: a topic page, a journal entry that no job wrote, or a knowledge
    page with a `question`."""
    if not index_path.startswith(_INDEX_WIKI_PREFIX):
        return None
    rel = index_path[len(_INDEX_WIKI_PREFIX) :]
    wiki = layout.wiki_root(data_root)
    path = wiki / rel
    if layout.is_hidden(path, wiki) or not path.is_file():
        return None
    first = rel.split("/", 1)[0]
    if "/" in rel and first in RESERVED_FOLDERS and first not in _RECORD_FOLDERS:
        return None
    if first == layout.KNOWLEDGE:
        return _is_answer_page(index_path, data_root)
    if first == "journal":
        meta, _ = frontmatter.split(read_page(path))
        return None if is_job_entry(meta) else rel
    return rel


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
        record_sim: float = DEFAULT_RECORD_SIM,
        asked: Sequence[AskedQuestion] = (),
        person_section: bool = False,
    ) -> None:
        self._bundle_text = bundle_text
        self._terms = [t for t in terms if t.strip()]
        self._people = [p for p in people if p.strip()]
        self._texts: list[str] = list(existing)
        self._store = store
        self._dup_sim = dup_sim
        self._data_root = Path(data_root)
        self._embed = embed
        self._record_sim = record_sim
        self._asked: list[AskedQuestion] = list(asked)
        self._person_section = person_section
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

    async def _record(self, question: str) -> str | None:
        """The closest of our records at `record_sim` or more, or None."""
        vectors = await self._vectors_of(question)
        if not vectors:
            return None
        try:
            near = await self._store.kb_near_paths(
                vectors, kinds=list(RECORD_KINDS), min_sim=self._record_sim
            )
        except Exception as exc:  # noqa: BLE001 — no index means no record
            self.notes.append("The record search did not run.")
            logger.warning({"message": "research record search failed", "error": str(exc)})
            return None
        for index_path, sim in sorted(near.items(), key=lambda item: (-item[1], item[0])):
            if sim < self._record_sim:
                continue
            page = _record_page(index_path, self._data_root)
            if page is not None:
                return page
        return None

    async def _asked_before(self, question: str) -> tuple[str, str | None]:
        """`already asked` or `dismissed before` with the question page, or
        `("", None)`."""
        mine = normalize(question)
        vectors = await self._vectors_of(question)
        for known in self._asked:
            same = normalize(known.question) == mine
            if not same and vectors:
                others = await self._vectors_of(known.question)
                if others:
                    same = max(cosine(a, b) for a in vectors for b in others) >= self._dup_sim
            if same:
                why = DISMISSED_BEFORE if known.status == "dismissed" else ALREADY_ASKED
                return why, known.path
        return "", None

    async def check(self, candidate: Candidate, *, person: bool = False) -> Verdict:
        """Check one candidate and give its route. With `person`, a person
        asked it: only the shape rules apply, and it goes to the dossier. A
        candidate that passed counts as known for the next."""
        question = candidate.question
        reason = shape_reason(question, self._terms)
        if reason:
            return Verdict(candidate, False, reason)
        if person:
            self._texts.append(question)
            return Verdict(candidate, True, route=WEB)
        reason = grounding_reason(candidate.grounded_in, self._bundle_text)
        if reason:
            return Verdict(candidate, False, reason)
        why, page = await self._duplicate(question)
        if why:
            return Verdict(candidate, False, why, answered_by=page)
        audience = candidate.audience if candidate.audience in AUDIENCES else WEB
        route_reason = world_rule(question, self._people)
        if route_reason:
            audience = PERSON
        elif self._person_section and audience != RECORD:
            audience, route_reason = PERSON, PERSON_SECTION
        elif candidate.about_person and audience != RECORD:
            audience, route_reason = PERSON, ABOUT_PERSON
        elif audience == PERSON:
            if not candidate.why.strip():
                return Verdict(candidate, False, "a question for a person needs `why`")
            route_reason = ROUTE_WORKER
        if audience == RECORD:
            record = await self._record(question)
            if record is not None:
                self._texts.append(question)
                return Verdict(candidate, True, answered_by=record, route=RECORD)
            audience, route_reason = PERSON, NO_RECORD
        if audience == PERSON:
            before, page = await self._asked_before(question)
            if before:
                return Verdict(
                    candidate, False, before, route=PERSON, route_reason=route_reason, page=page
                )
            self._asked.append(AskedQuestion(question, "open"))
            self._texts.append(question)
            return Verdict(candidate, True, route=PERSON, route_reason=route_reason)
        self._texts.append(question)
        return Verdict(candidate, True, route=WEB)
