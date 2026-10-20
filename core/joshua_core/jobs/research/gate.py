"""The specificity gate and the route: which raised questions a dossier
takes, which go to a person, and which our records answer.

A question from the `raise` worker has an audience: `web`, `person`, or
`record`.

A `web` question is a pair. `question` is the general question: the text that
goes to the open web. It holds the class of the thing and its public
specifics only. `local_context` holds our specifics and why we ask, and it
stays inside the instance. The gate checks a `web` question by these rules,
in this order, and rejects it at the first rule it fails:

1. The possessive forms of the title (`topics.possessive_forms`, in any case)
   become the generic name of the topic: "a soup like Alex's Caldo Verde"
   becomes "a soup like Caldo Verde", and `Verdict.generic_name` is true. The
   dossier and any page keep the new text.
2. The general question is 20 to 300 characters and ends with `?`.
3. It holds a name of the topic by the rule of `topics.names_pattern`, or
   the `subject` that the worker gives: a generic name of the thing, such as
   "a snake plant". The question holds the subject when it holds the subject
   as a phrase by the same rule, or when it holds the content words of the
   subject (see `holds_subject`). A `subject` that the question does not
   hold is a rejection that names the missing words. With no `subject`, the
   gate keeps the topic name it found as the subject.
4. `local_context` is 40 to 600 characters.
5. `grounded_in` has 2 phrases or more, each 3 to 160 characters, and each
   occurs verbatim in the bundle text. Case and white space do not count.
   There is no stemming and no fuzzy match.
6. The floor rules (`world_rule`): the general question does not name a
   person on the roster, by a display name or an id, as whole words in any
   case (`names a person`); holds no date in ISO form (`2026-10-04`) or in
   `Month D` form (`October 4`, `Oct 4`) (`names a record date`); does not
   start with `what did`, `why did`, `when did`, `will`, `should we`,
   `did we`, or `do we` (`asks what we did or will do`); and holds no `we`,
   `our`, or `us` as a word (`asks about us`). `US` in capitals is a country
   and does not count. A general question that fails a floor rule is
   rejected with the rule as its reason. It never goes to a person, because
   a question for a person has its own text.
7. It is not a duplicate: its normalized text is not the text of a question
   of the dossier, and its vector is below `jobs.research.dup_sim` to each
   question of the dossier and to each `kind: knowledge` chunk of the index,
   of any topic. A match on a published knowledge page of another topic is
   not a rejection: the question is accepted with `shared: true` and that
   page in `answered_by`, and the dossier closes it as `answered`. A match on
   a knowledge page of the same topic is a rejection that names that page in
   `answered_by`.

Then the route of a `web` question that passed. A topic in a person section
(`jobs.research.person_sections`) is about people, so its question goes to
`person` with `route_reason: person section`. A question with
`about_person: true` goes to `person` with `route_reason: about a person`:
the worker judges, and the floor rules still apply when the worker says
`false`. Every other `web` question goes to the dossier.

A `person` or a `record` question keeps its own `question` text, which may
name a person, and the pair rules do not apply to it. The topic rule does
not apply either, because the page of the question stores its topic and its
context. It must have the shape of rule 2 and pass rule 5 and rule 7 (with no
shared answer). When it fails a floor rule, it goes to `person`, and
`route_reason` names the rule. A question of a person section that is not
`record` goes to `person` (`person section`), and so does one with
`about_person: true` (`about a person`). A `record` question goes to the
memory search over the `wiki`, `journal`, and `knowledge` kinds: a hit at
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
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from joshua_shared import frontmatter, layout
from joshua_shared.log import get_logger

from joshua_core.jobs.bundle import BundleStore, is_job_entry
from joshua_core.jobs.research.known import (
    KNOWLEDGE_KIND,
    STATUS_PUBLISHED,
    Embedder,
    answer_page,
)
from joshua_core.jobs.research.text import collapse
from joshua_core.jobs.topics import RESERVED_FOLDERS, names_pattern, read_page
from joshua_core.memory.indexer import embed_document

logger = get_logger("jobs.research.gate")

MIN_PHRASES = 2
PHRASE_MIN_CHARS = 3
PHRASE_MAX_CHARS = 160
QUESTION_MIN_CHARS = 20
QUESTION_MAX_CHARS = 300
LOCAL_MIN_CHARS = 40
LOCAL_MAX_CHARS = 600
SUBJECT_MAX_CHARS = 80
# The words of a subject that are not content words.
SUBJECT_STOP_WORDS = frozenset(
    "a an the of for in on to and or with inside into from by at is are".split()
)
# A question holds a subject by its content words when it holds this many of
# them, and this share of them.
SUBJECT_MIN_WORDS = 2
SUBJECT_MIN_SHARE = 0.6
# The endings that a stem drops, in this order. A stem keeps at least
# `_STEM_MIN_CHARS` characters.
_STEM_ENDINGS = ("ing", "ed", "es", "s")
_STEM_MIN_CHARS = 3
_WORD_RE = re.compile(r"[^\W_]+")
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
# An apostrophe of a possessive form, straight or curly, and the class that
# matches either one.
_APOSTROPHE_RE = re.compile("['\u2019]")
_APOSTROPHE_CLASS = "['\u2019]"


@dataclass(frozen=True)
class Candidate:
    """A question to check, with its phrases, why it matters, its audience, and
    whether the worker says it carries a specific of a person.

    For a `web` question, `question` is the general question, `local_context`
    holds our specifics, and `subject` is the generic name of the thing that
    the question holds. A `person` or a `record` question has no
    `local_context`."""

    question: str
    grounded_in: list[str] = field(default_factory=list)
    why: str = ""
    audience: str = WEB
    about_person: bool = False
    local_context: str = ""
    subject: str = ""


@dataclass(frozen=True)
class Verdict:
    """The gate's answer for one candidate. `reason` is empty when it passed.

    `route` is `web`, `person`, or `record` for a candidate that passed the
    rules, and `route_reason` says why a question went to `person`. A
    `record` question names its record in `answered_by`. A `person` question
    that is not accepted, because it was asked before, names that question
    page in `page`. `generic_name` is true when the gate put the generic name
    of the topic in place of a possessive form of its title, and `candidate`
    then holds the new text. `shared` is true for a `web` question that a
    published knowledge page of another topic answers: `answered_by` names
    that page, relative to the wiki root.
    """

    candidate: Candidate
    accepted: bool
    reason: str = ""
    answered_by: str | None = None
    route: str = ""
    route_reason: str = ""
    page: str | None = None
    generic_name: bool = False
    shared: bool = False


@dataclass(frozen=True)
class _Duplicate:
    """What the duplicate check found: a reason to reject, or a page of
    another topic that answers the question, or neither."""

    why: str = ""
    page: str | None = None
    shared: bool = False


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


def generic_question(question: str, possessives: Sequence[str], generic: str) -> str:
    """`question` with `generic` in place of each of `possessives`, in any
    case and with any white space. Give it `topics.possessive_forms` and
    `topics.generic_title` of the topic title. With no `generic`, the question
    is unchanged."""
    if not generic.strip():
        return question
    text = question
    for form in sorted({f for f in possessives if f.strip()}, key=len, reverse=True):
        body = r"\s+".join(
            _APOSTROPHE_RE.sub(_APOSTROPHE_CLASS, re.escape(word)) for word in form.split()
        )
        text = re.sub(rf"(?<!\w){body}(?!\w)", lambda _: generic, text, flags=re.IGNORECASE)
    return text


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


def form_reason(question: str) -> str:
    """Why a question fails the length rule or ends with no question mark,
    or `""`."""
    text = question.strip()
    if not QUESTION_MIN_CHARS <= len(text) <= QUESTION_MAX_CHARS:
        return (
            f"the question has {len(text)} characters; it must have "
            f"{QUESTION_MIN_CHARS} to {QUESTION_MAX_CHARS}"
        )
    if not text.endswith("?"):
        return "the question does not end with a question mark"
    return ""


def shape_reason(question: str, terms: Sequence[str]) -> str:
    """Why a question fails the length or the topic rule, or `""`."""
    reason = form_reason(question)
    if reason:
        return reason
    if not names_topic(question.strip(), terms):
        return "the question does not name the topic"
    return ""


def _words(text: str) -> list[str]:
    """The words of `text` in lower case. A hyphen or a mark splits a word."""
    return _WORD_RE.findall(text.casefold())


def stem(word: str) -> str:
    """`word` without the first of `_STEM_ENDINGS` that it ends with, when at
    least `_STEM_MIN_CHARS` characters stay."""
    for ending in _STEM_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= _STEM_MIN_CHARS:
            return word[: -len(ending)]
    return word


def subject_words(subject: str) -> list[str]:
    """The content words of `subject`, once each, in order: its words in lower
    case, with no word of `SUBJECT_STOP_WORDS`."""
    return list(dict.fromkeys(w for w in _words(subject) if w not in SUBJECT_STOP_WORDS))


def missing_subject_words(question: str, subject: str) -> list[str]:
    """The content words of `subject` that `question` does not hold. The
    question holds a word when one of its words starts with the stem of it."""
    held = _words(question)
    return [w for w in subject_words(subject) if not any(h.startswith(stem(w)) for h in held)]


def holds_subject(question: str, subject: str) -> bool:
    """True when `question` holds `subject`: as a phrase by the rule of
    `topics.names_pattern`, or by its content words. By its content words,
    the question holds `SUBJECT_MIN_WORDS` of them or more, and
    `SUBJECT_MIN_SHARE` of them or more."""
    pattern = names_pattern([subject])
    if pattern is not None and pattern.search(question) is not None:
        return True
    words = subject_words(subject)
    found = len(words) - len(missing_subject_words(question, subject))
    return found >= SUBJECT_MIN_WORDS and found >= SUBJECT_MIN_SHARE * len(words)


def subject_reason(question: str, subject: str) -> str:
    """Why `question` does not hold `subject`, with the missing content
    words, or `""`."""
    if holds_subject(question, subject):
        return ""
    reason = f"the question does not hold its subject `{subject[:100]}`"
    missing = missing_subject_words(question, subject)
    if missing:
        return f"{reason}; missing words: {', '.join(f'`{w}`' for w in missing)}"
    return (
        f"{reason}; a subject with fewer than {SUBJECT_MIN_WORDS} content words "
        "must occur as a phrase"
    )


def subject_of(question: str, terms: Sequence[str], subject: str) -> tuple[str, str]:
    """The subject of a general question and why it fails the topic rule, as
    `(reason, subject)`. See rule 3 of the module docstring."""
    given = " ".join(subject.split())
    if given:
        if len(given) > SUBJECT_MAX_CHARS:
            return f"the subject has {len(given)} characters; at most {SUBJECT_MAX_CHARS}", ""
        reason = subject_reason(question, given)
        return (reason, "") if reason else ("", given)
    pattern = names_pattern(terms)
    found = pattern.search(question) if pattern is not None else None
    if found is None:
        return "the question does not name the topic or a subject", ""
    return "", " ".join(found.group(0).split())


def general_shape_reason(question: str, terms: Sequence[str], subject: str) -> tuple[str, str]:
    """Why a general question fails the length rule or the topic rule, or
    `""`, and its subject, as `(reason, subject)`."""
    reason = form_reason(question)
    if reason:
        return reason, ""
    return subject_of(question.strip(), terms, subject)


def local_context_reason(text: str) -> str:
    """Why the `local_context` of a `web` question fails, or `""`."""
    size = len(" ".join(text.split()))
    if not LOCAL_MIN_CHARS <= size <= LOCAL_MAX_CHARS:
        return (
            f"local_context has {size} characters; it must have "
            f"{LOCAL_MIN_CHARS} to {LOCAL_MAX_CHARS}"
        )
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
    found = answer_page(index_path, data_root)
    return found[0] if found is not None else None


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
        possessives: Sequence[str] = (),
        generic: str = "",
        topic: str = "",
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
        self._possessives = [p for p in possessives if p.strip()]
        self._generic = generic
        self._topic = topic
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

    async def _duplicate(self, question: str, *, share: bool = False) -> _Duplicate:
        """Why `question` is a duplicate and of which page. With `share`, a
        published knowledge page of another topic is a shared answer, not a
        duplicate."""
        mine = normalize(question)
        for known in self._texts:
            if normalize(known) == mine:
                return _Duplicate("the same question is in the dossier")
        vectors = await self._vectors_of(question)
        if not vectors:
            return _Duplicate()
        for known in self._texts:
            others = await self._vectors_of(known)
            if not others:
                continue
            best = max(cosine(a, b) for a in vectors for b in others)
            if best >= self._dup_sim:
                return _Duplicate(
                    f"a question of the dossier is this close ({best:.2f}): `{known[:120]}`"
                )
        try:
            near = await self._store.kb_near_paths(
                vectors, kinds=[KNOWLEDGE_KIND], min_sim=self._dup_sim
            )
        except Exception as exc:  # noqa: BLE001 — no index means no index check
            self.notes.append("The index check for duplicates did not run.")
            logger.warning({"message": "research gate index failed", "error": str(exc)})
            return _Duplicate()
        for index_path, sim in sorted(near.items(), key=lambda item: (-item[1], item[0])):
            found = answer_page(index_path, self._data_root)
            if found is None:
                continue
            page, meta = found
            other = (
                share
                and bool(self._topic)
                and meta.get("topic") != self._topic
                and meta.get("status") == STATUS_PUBLISHED
            )
            if other:
                return _Duplicate(page=page, shared=True)
            return _Duplicate(f"a knowledge page is this close ({sim:.2f})", page)
        return _Duplicate()

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
        candidate that passed counts as known for the next. A `web` candidate
        from the worker is checked as a pair (see the module docstring)."""
        audience = candidate.audience if candidate.audience in AUDIENCES else WEB
        if person or audience != WEB:
            return await self._check(candidate, person=person)
        question = generic_question(candidate.question, self._possessives, self._generic)
        verdict = await self._check_web(replace(candidate, question=question, audience=WEB))
        return replace(verdict, generic_name=question != candidate.question)

    async def _check_web(self, candidate: Candidate) -> Verdict:
        question = candidate.question
        reason, subject = general_shape_reason(question, self._terms, candidate.subject)
        if reason:
            return Verdict(candidate, False, reason)
        candidate = replace(
            candidate, subject=subject, local_context=" ".join(candidate.local_context.split())
        )
        reason = local_context_reason(candidate.local_context) or grounding_reason(
            candidate.grounded_in, self._bundle_text
        )
        if reason:
            return Verdict(candidate, False, reason)
        rule = world_rule(question, self._people)
        if rule:
            return Verdict(candidate, False, f"{rule}: {world_reason(question, self._people)}")
        duplicate = await self._duplicate(question, share=True)
        if duplicate.shared:
            self._texts.append(question)
            return Verdict(candidate, True, answered_by=duplicate.page, route=WEB, shared=True)
        if duplicate.why:
            return Verdict(candidate, False, duplicate.why, answered_by=duplicate.page)
        if self._person_section:
            return await self._to_person(candidate, PERSON_SECTION)
        if candidate.about_person:
            return await self._to_person(candidate, ABOUT_PERSON)
        self._texts.append(question)
        return Verdict(candidate, True, route=WEB)

    async def _to_person(self, candidate: Candidate, route_reason: str) -> Verdict:
        """The verdict of a question that goes to a person: `already asked`,
        `dismissed before`, or accepted."""
        question = candidate.question
        before, page = await self._asked_before(question)
        if before:
            return Verdict(
                candidate, False, before, route=PERSON, route_reason=route_reason, page=page
            )
        self._asked.append(AskedQuestion(question, "open"))
        self._texts.append(question)
        return Verdict(candidate, True, route=PERSON, route_reason=route_reason)

    async def _check(self, candidate: Candidate, *, person: bool) -> Verdict:
        question = candidate.question
        reason = shape_reason(question, self._terms) if person else form_reason(question)
        if reason:
            return Verdict(candidate, False, reason)
        if person:
            self._texts.append(question)
            return Verdict(candidate, True, route=WEB)
        reason = grounding_reason(candidate.grounded_in, self._bundle_text)
        if reason:
            return Verdict(candidate, False, reason)
        duplicate = await self._duplicate(question)
        if duplicate.why:
            return Verdict(candidate, False, duplicate.why, answered_by=duplicate.page)
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
        return await self._to_person(candidate, route_reason)
