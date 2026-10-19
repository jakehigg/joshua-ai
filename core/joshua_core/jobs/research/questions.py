"""The questions for a person: one page each, and the list of every question.

A question for a person is a question that the researcher could not answer
from the web or from our records. It is at `layout.question_page(section,
slug)`. Its frontmatter is the `QuestionPage` model. Python writes the body
from the frontmatter, under fixed headings, in this order: the question as the
title, **Why Joshua wants to know**, **Where it came from**, and **Answer**.
No worker writes a page.

`questions/README.md` (`layout.questions_index`) is the list of every
question. `write_index` writes it again on each write of the researcher and of
the `questions` chat tool.

Joshua never sends a question. A person answers one in chat, with the
`questions` chat tool.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from joshua_shared import frontmatter, layout
from joshua_shared.log import get_logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from joshua_core.jobs.research.dossier import question_slug
from joshua_core.jobs.research.text import inline, strip_links
from joshua_core.jobs.topics import read_page, section_slug

logger = get_logger("jobs.research.questions")

SOURCE = "research"
ASKED_BY = "research"
EVERYONE = "everyone"
QuestionStatus = Literal["open", "answered", "dismissed"]
STATUSES: tuple[str, ...] = ("open", "answered", "dismissed")

HEADING_WHY = "Why Joshua wants to know"
HEADING_FROM = "Where it came from"
HEADING_ANSWER = "Answer"
HEADINGS = (HEADING_WHY, HEADING_FROM, HEADING_ANSWER)
# The most characters of an answer that a page keeps.
MAX_ANSWER = 4000

# The mark of a Markdown heading line, so a person's text cannot add a heading.
_HEADING_MARK_RE = re.compile(r"^[ \t]{0,3}#{1,6}(?:[ \t]+|$)", re.MULTILINE)
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


class QuestionPage(BaseModel):
    """The frontmatter of a question page. A key this model does not know is kept.

    `for_` is the key `for`: `everyone`, or the id of the person who would know.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    question: str
    topic: str
    section: str
    why: str = ""
    for_: str = Field(default=EVERYONE, alias="for")
    context: list[str] = Field(default_factory=list)
    grounded_in: list[str] = Field(default_factory=list)
    route_reason: str = ""
    status: QuestionStatus = "open"
    asked_by: str = ASKED_BY
    raised_at: date
    answered_at: date | None = None
    answered_by: str | None = None
    answer: str | None = None
    source: str = SOURCE
    run_id: int | None = None


@dataclass(frozen=True)
class QuestionFile:
    """A question on the disk: its id (`<section>/<slug>`), its path, its
    frontmatter, and its whole text."""

    id: str
    path: Path
    page: QuestionPage
    text: str

    @property
    def rel(self) -> str:
        """The path of the page relative to the wiki root."""
        return question_rel(self.id)


# -- paths --------------------------------------------------------------------


def question_section(topic_rel: str) -> str:
    """The question section of a topic: its first path segment as a slug, or
    `general` for a page at the wiki root."""
    return section_slug(topic_rel)


def question_rel(question_id: str) -> str:
    """The path of a question page relative to the wiki root."""
    return f"{layout.QUESTIONS}/{question_id}.md"


def question_path(question_id: str, data_root: Path | str) -> Path:
    """The path of `<section>/<slug>`. Raises `ValueError` for any other shape."""
    parts = question_id.split("/") if isinstance(question_id, str) else []
    if len(parts) != 2:
        raise ValueError(f"not a question id: {question_id!r}")
    return layout.question_page(parts[0], parts[1], data_root)


def _rel_link(from_file: str, to_file: str) -> str:
    start = PurePosixPath(from_file).parent.as_posix()
    return os.path.relpath(to_file, start if start != "." else os.curdir).replace(os.sep, "/")


def new_slug(section: str, question: str, data_root: Path | str, taken: Iterable[str] = ()) -> str:
    """The slug of a new question: `dossier.question_slug` of its text, unique
    among the pages of the section on the disk and `taken`."""
    folder = layout.questions_root(data_root) / section
    used = set(taken)
    if folder.is_dir():
        used.update(p.stem for p in folder.glob("*.md"))
    return question_slug(question, used)


# -- who would know -------------------------------------------------------------


def who_would_know(texts: Sequence[str], people: Sequence[tuple[str, str]]) -> str:
    """The id of the one person that `texts` name, or `everyone`.

    `people` holds `(id, display name)` for each person on the roster. A text
    names a person when it holds the display name or the id as whole words, in
    any case. When the texts name no person, or more than one, the answer is
    `everyone`.
    """
    found: list[str] = []
    joined = " ".join(" ".join(t.split()) for t in texts)
    for pid, name in people:
        for term in (name, pid):
            words = r"\s+".join(re.escape(w) for w in str(term).split())
            if words and re.search(rf"(?<!\w){words}(?!\w)", joined, re.IGNORECASE):
                found.append(pid)
                break
    unique = list(dict.fromkeys(found))
    return unique[0] if len(unique) == 1 else EVERYONE


def roster(settings: Any) -> list[tuple[str, str]]:
    """`(id, display name)` for each person in `settings.people`."""
    return [(p.id, p.name) for p in getattr(settings, "people", None) or []]


# -- read -----------------------------------------------------------------------


def parse_question(text: str) -> QuestionPage | None:
    """The question in a page text, or None when its frontmatter is not one."""
    meta, _ = frontmatter.split(text)
    if not isinstance(meta.get("question"), str) or not isinstance(meta.get("topic"), str):
        return None
    try:
        return QuestionPage.model_validate(meta)
    except ValidationError:
        return None


def load_question(question_id: str, data_root: Path | str) -> QuestionFile | None:
    """One question by its id, or None when there is none or it does not parse."""
    try:
        path = question_path(question_id, data_root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    text = read_page(path)
    page = parse_question(text)
    if page is None:
        return None
    return QuestionFile(question_id, path, page, text)


def load_questions(data_root: Path | str, topic: str | None = None) -> list[QuestionFile]:
    """Every question under `wiki/questions/<section>/`, sorted by id. With
    `topic`, only the questions whose `topic` is that page."""
    root = layout.questions_root(data_root)
    found: list[QuestionFile] = []
    if not root.is_dir():
        return found
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        for page in sorted(folder.glob("*.md")):
            if page.name.startswith(".") or not page.is_file():
                continue
            loaded = load_question(f"{folder.name}/{page.stem}", data_root)
            if loaded is None:
                continue
            if topic is None or loaded.page.topic == topic:
                found.append(loaded)
    return found


def counts(data_root: Path | str, topic: str) -> tuple[int, int]:
    """The open and the answered questions of `topic`."""
    pages = load_questions(data_root, topic)
    return (
        sum(1 for q in pages if q.page.status == "open"),
        sum(1 for q in pages if q.page.status == "answered"),
    )


# -- write ----------------------------------------------------------------------


def _plain(text: str) -> str:
    """Text with no link and no heading line."""
    return _HEADING_MARK_RE.sub("", strip_links(text.replace("\r\n", "\n"), set())).strip()


def person_text(text: str, me: str, data_root: Path | str) -> str:
    """The answer of a person as page text: no heading line, and no link
    except a Markdown link to a page of the wiki that is on the disk, which
    gets a target relative to the page `me`."""
    wiki = layout.wiki_root(data_root)

    def rewrite(target: str) -> str | None:
        if _SCHEME_RE.match(target) or target.startswith("//") or "\\" in target:
            return None
        clean = target.split("#", 1)[0].lstrip("/")
        while clean.startswith("../"):
            clean = clean[3:]
        parts = clean.split("/")
        if not clean.endswith(".md") or any(p in ("", ".", "..") for p in parts):
            return None
        path = wiki / clean
        if layout.is_hidden(path, wiki) or not path.is_file():
            return None
        return _rel_link(me, clean)

    text = strip_links(text.replace("\r\n", "\n"), set(), rewrite=rewrite)
    return _HEADING_MARK_RE.sub("", text).strip()


def render_body(question_id: str, page: QuestionPage, data_root: Path | str) -> str:
    """The body of a question page."""
    me = question_rel(question_id)
    lines = [f"# {inline(_plain(page.question), 300)}", ""]
    lines += [f"## {HEADING_WHY}", "", _plain(page.why) or "Not given.", ""]
    lines += [f"## {HEADING_FROM}", ""]
    for path in page.context or [page.topic]:
        lines.append(f"- [{path}]({_rel_link(me, path)})")
    lines += ["", f"## {HEADING_ANSWER}", ""]
    if page.status == "answered" and page.answer:
        lines.append(person_text(page.answer, me, data_root))
    elif page.status == "dismissed":
        why = person_text(page.answer or "", me, data_root)
        lines.append(f"Dismissed: {why}" if why else "Dismissed.")
    return "\n".join(lines).rstrip() + "\n"


def _meta(page: QuestionPage) -> dict[str, Any]:
    return page.model_dump(mode="python", by_alias=True)


def render_question(question_id: str, page: QuestionPage, data_root: Path | str) -> str:
    """The whole text of a question page: the frontmatter and the body."""
    return frontmatter.update(render_body(question_id, page, data_root), **_meta(page))


def write_text(path: Path, text: str, data_root: Path | str) -> Path:
    """Write a file of the questions. Refuses a path outside `wiki/questions/`."""
    root = layout.questions_root(data_root).resolve()
    target = path.resolve()
    if root not in target.parents:
        raise ValueError(f"not under wiki/questions: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_question(question_id: str, page: QuestionPage, data_root: Path | str) -> Path:
    """Write one question page and return its path."""
    path = question_path(question_id, data_root)
    return write_text(path, render_question(question_id, page, data_root), data_root)


def set_answer(
    current: QuestionFile,
    data_root: Path | str,
    *,
    status: str,
    answer: str,
    by: str,
    day: date,
) -> QuestionFile:
    """Write `status`, `answer`, `answered_by`, and `answered_at` onto a
    question page, and its body again. Raises `ValueError` for a value the
    model refuses."""
    try:
        page = QuestionPage.model_validate(
            {
                **_meta(current.page),
                "status": status,
                "answer": answer[:MAX_ANSWER],
                "answered_by": by,
                "answered_at": day,
            }
        )
    except ValidationError as exc:
        raise ValueError(f"not a valid question: {exc.error_count()} errors") from None
    text = render_question(current.id, page, data_root)
    write_text(current.path, text, data_root)
    return QuestionFile(current.id, current.path, page, text)


# -- the index ------------------------------------------------------------------


def _cell(text: str) -> str:
    return inline(text).replace("|", "\\|")


def open_order(items: Iterable[QuestionFile]) -> list[QuestionFile]:
    """The open questions, the oldest first."""
    found = sorted((q for q in items if q.page.status == "open"), key=lambda q: q.id)
    found.sort(key=lambda q: q.page.raised_at)
    return found


def closed_order(items: Iterable[QuestionFile]) -> list[QuestionFile]:
    """The answered and the dismissed questions, the newest first."""
    found = sorted((q for q in items if q.page.status != "open"), key=lambda q: q.id)
    found.sort(key=lambda q: q.page.answered_at or q.page.raised_at, reverse=True)
    return found


def render_index(items: Iterable[QuestionFile]) -> str:
    """The text of `questions/README.md`."""
    me = f"{layout.QUESTIONS}/README.md"
    pages = list(items)
    lines = [
        "# Questions",
        "",
        "These are questions that the researcher could not answer from the web or "
        "from our records. Joshua does not send them to anybody. A person answers "
        "one in chat, and the researcher reads the answer on its next run. The "
        "researcher writes this page again on each change.",
        "",
        "## Open",
        "",
        "| Question | Topic | For | Raised |",
        "|---|---|---|---|",
    ]
    opened = open_order(pages)
    for item in opened:
        q = item.page
        question = f"[{_cell(_plain(q.question))}]({_rel_link(me, item.rel)})"
        topic = f"[{_cell(q.topic)}]({_rel_link(me, q.topic)})"
        lines.append(f"| {question} | {topic} | {_cell(q.for_)} | {q.raised_at.isoformat()} |")
    if not opened:
        lines.append("| none | | | |")
    lines += [
        "",
        "## Answered and dismissed",
        "",
        "| Status | Question | Topic | By | Day |",
        "|---|---|---|---|---|",
    ]
    closed = closed_order(pages)
    for item in closed:
        q = item.page
        question = f"[{_cell(_plain(q.question))}]({_rel_link(me, item.rel)})"
        topic = f"[{_cell(q.topic)}]({_rel_link(me, q.topic)})"
        day = (q.answered_at or q.raised_at).isoformat()
        lines.append(
            f"| {q.status} | {question} | {topic} | {_cell(q.answered_by or '')} | {day} |"
        )
    if not closed:
        lines.append("| none | | | | |")
    return frontmatter.update("\n".join(lines) + "\n", source=SOURCE)


def write_index(data_root: Path | str) -> Path:
    """Write `questions/README.md` from every question on the disk."""
    text = render_index(load_questions(data_root))
    return write_text(layout.questions_index(data_root), text, data_root)
