"""The dossier of a topic, and the table of every dossier.

A dossier is the state of the researcher for one topic, at
`layout.knowledge_dossier(topic)`. Its frontmatter is the `Dossier` model: the
topic, its questions, and when it was last researched. Python writes its body
again on each save, from the frontmatter and the pages of the answered
questions. No worker writes a dossier.

A question holds the general question, which went to the web, in `question`,
and our specifics in `local_context`, which stays inside the instance.
`subject` is the generic name of the thing that the general question holds. A
question that a published page of another topic answers is `answered` with
`shared: true`, and its `page` is that page. A dossier from before these
fields loads with `local_context: ""`, `subject: ""`, and `shared: false`.

A question that was held or dropped after its lookups ran keeps the answers of
the two lookups in `draft` and `challenge`, and the day of the lookups in
`looked_up_at`. The body shows the draft of a held question in a collapsed
section.

`knowledge/README.md` (`layout.knowledge_index`) is the table of every
dossier. `write_index` writes it again on each write of the researcher.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Literal

from joshua_shared import frontmatter, layout
from joshua_shared.log import get_logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from joshua_core.engine.internet import InternetAnswer
from joshua_core.jobs.research.text import code_span, fenced, inline, strip_links
from joshua_core.jobs.topics import read_page

logger = get_logger("jobs.research.dossier")

SOURCE = "research"
ASKED_BY_RESEARCH = "research"
QuestionStatus = Literal["open", "answered", "held", "dropped"]
STATUSES: tuple[str, ...] = ("open", "answered", "held", "dropped")

# A slug holds at most this many words of the question.
_SLUG_WORDS = 6
_SLUG_MAX = 56
# The words that a slug leaves out, because they do not say what the question
# is about.
_STOP_WORDS = frozenset(
    """
    a an the of to in on for and or nor but is are was were be been being do does
    did how what when where which who whom whose why should would could can may
    might must shall will i we our ours us you your my me it its this that these
    those there their they them at by from with as into than then so if not no
    yes about over under any some each per vs versus
    """.split()
)
_WORD_RE = re.compile(r"[a-z0-9]+")


class Question(BaseModel):
    """One question of a dossier. `question` is the general question."""

    model_config = ConfigDict(extra="allow")

    slug: str
    question: str
    local_context: str = ""
    subject: str = ""
    shared: bool = False
    status: QuestionStatus = "open"
    page: str | None = None
    grounded_in: list[str] = Field(default_factory=list)
    asked_by: str = ASKED_BY_RESEARCH
    holds: int = 0
    hold_reason: str = ""
    raised_at: date | None = None
    draft: InternetAnswer | None = None
    challenge: InternetAnswer | None = None
    looked_up_at: date | None = None

    def keep_lookups(
        self, draft: InternetAnswer | None, challenge: InternetAnswer | None, day: date
    ) -> None:
        """Keep the answers of the two lookups of `day`, or clear them when
        the researcher gave none."""
        self.draft = draft
        self.challenge = challenge if draft is not None else None
        self.looked_up_at = day if draft is not None else None

    def clear_lookups(self) -> None:
        self.draft = None
        self.challenge = None
        self.looked_up_at = None

    def kept_lookups(
        self, day: date, max_age_days: int
    ) -> tuple[InternetAnswer, InternetAnswer] | None:
        """The draft and the challenge to give the editor again, or None.

        Only a held question with both answers gives them, and only when the
        lookups are not older than `max_age_days` on `day`.
        """
        if self.status != "held" or self.draft is None or self.challenge is None:
            return None
        if self.looked_up_at is None or (day - self.looked_up_at).days > max_age_days:
            return None
        return self.draft, self.challenge

    @property
    def by_person(self) -> bool:
        """True when a person asked the question, not the researcher."""
        return self.asked_by != ASKED_BY_RESEARCH


class Dossier(BaseModel):
    """The frontmatter of a dossier. A key this model does not know is kept.

    `person_open` and `person_answered` count the questions of the topic under
    `wiki/questions/`. The dossier holds only these counts, so it stays about
    the research on the web.
    """

    model_config = ConfigDict(extra="allow")

    topic: str
    title: str = ""
    researched_at: date | None = None
    refresh_after: date | None = None
    runs: int = 0
    coverage: float = 0.0
    questions: list[Question] = Field(default_factory=list)
    person_open: int = 0
    person_answered: int = 0
    source: str = SOURCE

    def count(self, status: str) -> int:
        return sum(1 for q in self.questions if q.status == status)

    def find(self, slug: str) -> Question | None:
        return next((q for q in self.questions if q.slug == slug), None)

    def shared_count(self) -> int:
        """The answered questions that a page of another topic answers."""
        return sum(1 for q in self.questions if q.status == "answered" and q.shared)

    def compute_coverage(self) -> float:
        """`answered / len(questions)`, or 0.0 when there is no question."""
        if not self.questions:
            return 0.0
        return round(self.count("answered") / len(self.questions), 4)

    def has_person_question(self) -> bool:
        """True when a person asked a question that is still open."""
        return any(q.status == "open" and q.by_person for q in self.questions)


def question_slug(text: str, taken: Iterable[str]) -> str:
    """The slug of a question: its first six content words, slugified, and
    unique among `taken` by a `-2`, `-3`, ... suffix."""
    words = [w for w in _WORD_RE.findall(text.lower()) if w not in _STOP_WORDS]
    base = "-".join(words[:_SLUG_WORDS])[:_SLUG_MAX].strip("-") or "question"
    used = set(taken)
    slug = base
    number = 2
    while slug in used:
        slug = f"{base}-{number}"
        number += 1
    return slug


# -- read --------------------------------------------------------------------


def dossier_path(topic_rel: str, data_root: Path | str) -> Path:
    return layout.knowledge_dossier(topic_rel, data_root)


def parse_dossier(text: str) -> Dossier | None:
    """The dossier in a page text, or None when its frontmatter is not one."""
    meta, _ = frontmatter.split(text)
    if not isinstance(meta.get("topic"), str) or not isinstance(meta.get("questions", []), list):
        return None
    try:
        return Dossier.model_validate(meta)
    except ValidationError:
        return None


def load_dossier(topic_rel: str, data_root: Path | str) -> Dossier | None:
    """The dossier of a topic, or None when there is none or it does not parse."""
    path = dossier_path(topic_rel, data_root)
    if not path.is_file():
        return None
    dossier = parse_dossier(read_page(path))
    if dossier is None or dossier.topic != topic_rel:
        logger.warning({"message": "dossier not read", "topic": topic_rel})
        return None
    return dossier


def load_dossiers(data_root: Path | str) -> dict[str, Dossier]:
    """Every dossier under `wiki/knowledge/`, by topic path.

    A page counts only when it is at the dossier path of the topic its
    frontmatter names, so a knowledge page is never read as a dossier.
    """
    root = layout.knowledge_root(data_root)
    found: dict[str, Dossier] = {}
    if not root.is_dir():
        return found
    index = layout.knowledge_index(data_root)
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            path = Path(current) / name
            if not name.endswith(".md") or name.startswith(".") or path == index:
                continue
            dossier = parse_dossier(read_page(path))
            if dossier is None:
                continue
            try:
                expected = dossier_path(dossier.topic, data_root)
            except ValueError:
                continue
            if expected == path:
                found[dossier.topic] = dossier
    return found


def new_dossier(topic_rel: str, title: str) -> Dossier:
    return Dossier(topic=topic_rel, title=title)


# -- write -------------------------------------------------------------------


def rel_link(from_file: str, to_file: str) -> str:
    """A relative link from one wiki-relative file to another."""
    start = PurePosixPath(from_file).parent.as_posix()
    return os.path.relpath(to_file, start if start != "." else os.curdir).replace(os.sep, "/")


_rel_link = rel_link


def _cell(text: str) -> str:
    return inline(text).replace("|", "\\|")


def _dossier_rel(topic_rel: str) -> str:
    stem = topic_rel[:-3] if topic_rel.endswith(".md") else topic_rel
    return f"{layout.KNOWLEDGE}/{stem}.md"


def answer_short_of(page: str, data_root: Path | str) -> str:
    """The `answer_short` of a knowledge page on the disk, or `""`. `page` is
    relative to `wiki/knowledge/`."""
    path = layout.knowledge_root(data_root) / page
    if layout.is_hidden(path, layout.knowledge_root(data_root)) or ".." in page.split("/"):
        return ""
    meta, _ = frontmatter.split(read_page(path))
    value = meta.get("answer_short")
    return value if isinstance(value, str) else ""


def render_body(dossier: Dossier, answers: Mapping[str, str]) -> str:
    """The body of a dossier. `answers` maps the `page` of an answered
    question, relative to `wiki/knowledge/`, to its one-line short answer."""
    me = _dossier_rel(dossier.topic)
    title = inline(dossier.title) or dossier.topic
    lines = [
        f"# {title}: what we researched",
        "",
        f"Topic page: [{title}]({_rel_link(me, dossier.topic)}). "
        f"Coverage: {dossier.coverage:.2f}, {dossier.count('answered')} of "
        f"{len(dossier.questions)} questions answered.",
        "",
    ]
    if dossier.person_open or dossier.person_answered:
        index = _rel_link(me, f"{layout.QUESTIONS}/README.md")
        lines += [
            f"Questions for a person: {dossier.person_open} open, "
            f"{dossier.person_answered} answered. See [the list]({index}).",
            "",
        ]
    lines += [
        "## Answered",
        "",
    ]
    answered = [q for q in dossier.questions if q.status == "answered" and q.page]
    for q in answered:
        assert q.page is not None
        short = inline(strip_links(answers.get(q.page, ""), set()))
        tail = f": {short}" if short else ""
        target = _rel_link(me, f"{layout.KNOWLEDGE}/{q.page}")
        mark = " (shared with another topic)" if q.shared else ""
        lines.append(f"- [{inline(strip_links(q.question, set()))}]({target}){mark}{tail}")
    if not answered:
        lines.append("None yet.")
    for heading, status in (("Open", "open"), ("Held", "held")):
        lines += ["", f"## {heading}", ""]
        items = [q for q in dossier.questions if q.status == status]
        for q in items:
            reason = (
                f" Held: {inline(q.hold_reason)}." if status == "held" and q.hold_reason else ""
            )
            lines.append(f"- `{q.slug}`: {inline(strip_links(q.question, set()))}{reason}")
            if status == "held" and q.draft is not None:
                lines += _draft_lines(q)
        if not items:
            lines.append("None.")
    return "\n".join(lines) + "\n"


def _draft_lines(question: Question) -> list[str]:
    """The draft answer of a held question, in a collapsed section.

    The answer is text from the open web, so it is in a code block, with no
    link, and each source is a code span.
    """
    draft = question.draft
    assert draft is not None
    day = question.looked_up_at.isoformat() if question.looked_up_at else "an earlier run"
    sources = [f"- {code_span(s.url, 300)}" for s in draft.sources] or ["- None."]
    block = fenced(strip_links(draft.answer, set()))
    indented = "\n".join(f"  {line}" if line else "" for line in block.split("\n"))
    return [
        "",
        "  <details>",
        f"  <summary>The draft answer of {day}, confidence {draft.confidence}</summary>",
        "",
        indented,
        "",
        "  Sources:",
        "",
        *[f"  {line}" for line in sources],
        "",
        "  </details>",
        "",
    ]


def render_dossier(dossier: Dossier, answers: Mapping[str, str]) -> str:
    """The whole text of a dossier: the frontmatter and the body."""
    dossier.coverage = dossier.compute_coverage()
    meta = dossier.model_dump(mode="python", exclude_none=True)
    meta["questions"] = [q.model_dump(mode="python", exclude_none=True) for q in dossier.questions]
    return frontmatter.update(render_body(dossier, answers), **meta)


@dataclass(frozen=True)
class IndexRow:
    topic: str
    coverage: float
    answered: int
    total: int
    researched_at: date | None
    open: int
    shared: int = 0


def index_rows(dossiers: Iterable[Dossier]) -> list[IndexRow]:
    rows = [
        IndexRow(
            topic=d.topic,
            coverage=d.compute_coverage(),
            answered=d.count("answered"),
            shared=d.shared_count(),
            total=len(d.questions),
            researched_at=d.researched_at,
            open=d.count("open"),
        )
        for d in dossiers
    ]
    return sorted(rows, key=lambda r: r.topic)


def render_index(dossiers: Iterable[Dossier]) -> str:
    """The text of `knowledge/README.md`: one table row for each dossier,
    sorted by topic path."""
    lines = [
        "# Knowledge",
        "",
        "What the researcher found, one dossier for each topic. The researcher "
        "writes this page again on each run. Shared counts the answers that a "
        "page of another topic gives.",
        "",
        "| Topic | Coverage | Answered | Shared | Last researched | Open |",
        "|---|---|---|---|---|---|",
    ]
    rows = index_rows(dossiers)
    for row in rows:
        link = _rel_link(f"{layout.KNOWLEDGE}/README.md", _dossier_rel(row.topic))
        day = row.researched_at.isoformat() if row.researched_at else "never"
        lines.append(
            f"| [{_cell(row.topic)}]({link}) | {row.coverage:.2f} | "
            f"{row.answered}/{row.total} | {row.shared} | {day} | {row.open} |"
        )
    if not rows:
        lines.append("| none | | | | | |")
    return frontmatter.update("\n".join(lines) + "\n", source=SOURCE)


def write_text(path: Path, text: str, data_root: Path | str) -> Path:
    """Write a file of the researcher. Refuses a path outside `wiki/knowledge/`."""
    root = layout.knowledge_root(data_root).resolve()
    target = path.resolve()
    if root not in target.parents:
        raise ValueError(f"not under wiki/knowledge: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def save_dossier(dossier: Dossier, data_root: Path | str, answers: Mapping[str, str]) -> Path:
    """Write the dossier and return its path."""
    path = dossier_path(dossier.topic, data_root)
    return write_text(path, render_dossier(dossier, answers), data_root)


def write_index(data_root: Path | str) -> Path:
    """Write `knowledge/README.md` from every dossier on the disk."""
    dossiers = load_dossiers(data_root).values()
    return write_text(layout.knowledge_index(data_root), render_index(dossiers), data_root)


def answers_on_disk(dossier: Dossier, data_root: Path | str) -> dict[str, str]:
    """The `answer_short` of each answered page of the dossier, from the disk."""
    return {
        q.page: answer_short_of(q.page, data_root)
        for q in dossier.questions
        if q.status == "answered" and q.page
    }
