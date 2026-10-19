"""One knowledge page: the answer to one question of a topic.

The page is at `layout.knowledge_page(topic, slug)`. Python writes the
frontmatter and puts the parts of the editor's answer under fixed headings,
in this order: the question as the title, **Answer**, **Where this differs
for us** (when there is a local note), **Evidence**, **Disagreements** (when
there are some), and **Sources**.

A link in the text of a worker stays only when its URL is one of the sources
of the page. Every other link, picture, and wikilink is reduced to its text,
so a worker cannot put a link into the wiki. A page is 64 KB or less.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from joshua_shared import frontmatter, layout

from joshua_core.jobs.research.crew import EditorAnswer
from joshua_core.jobs.research.dossier import SOURCE, write_text
from joshua_core.jobs.research.text import allowed_urls, inline, strip_links

MAX_PAGE_BYTES = 64 * 1024
STATUS_PUBLISHED = "published"


class PageTooLong(ValueError):
    """The page is longer than `MAX_PAGE_BYTES`."""


@dataclass(frozen=True)
class Page:
    """A page to write: its path, its path relative to `wiki/knowledge/`, and its text."""

    path: Path
    rel: str
    text: str


def _block(text: str, allowed: set[str]) -> str:
    return strip_links(text.replace("\r\n", "\n"), allowed).strip()


def render_page(
    *,
    question: str,
    topic: str,
    context: Sequence[str],
    grounded_in: Sequence[str],
    answer: EditorAnswer,
    day: date,
    refresh_days: int,
    run_id: int | None,
) -> str:
    """The text of a knowledge page: the frontmatter and the body."""
    allowed = allowed_urls([s.url for s in answer.sources])
    title = inline(strip_links(question, set()), 300)
    lines = [f"# {title}", "", "## Answer", "", _block(answer.answer, allowed), ""]
    local = _block(answer.local_notes, allowed)
    if local:
        lines += ["## Where this differs for us", "", local, ""]
    lines += ["## Evidence", ""]
    evidence = [inline(strip_links(item, allowed), 2000) for item in answer.evidence]
    lines += [f"- {item}" for item in evidence if item] or ["None given."]
    lines.append("")
    disagreements = _block(answer.disagreements, allowed)
    if disagreements:
        lines += ["## Disagreements", "", disagreements, ""]
    lines += ["## Sources", ""]
    for source in answer.sources:
        name = inline(strip_links(source.title, set()), 300).replace("[", "(").replace("]", ")")
        target = source.url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")
        lines.append(f"- [{name or target}]({target}), {source.trust}")
    body = "\n".join(lines).rstrip() + "\n"
    meta = {
        "question": title,
        "answer_short": inline(strip_links(answer.answer_short, set()), 160),
        "topic": topic,
        "context": list(context),
        "grounded_in": list(grounded_in),
        "researched_at": day,
        "refresh_after": day + timedelta(days=refresh_days),
        "confidence": answer.confidence,
        "status": STATUS_PUBLISHED,
        "sources": [
            {"url": s.url, "title": inline(s.title, 300), "trust": s.trust, "retrieved": day}
            for s in answer.sources
        ],
        "source": SOURCE,
        "run_id": run_id,
    }
    if run_id is None:
        del meta["run_id"]
    text = frontmatter.update(body, **meta)
    if len(text.encode("utf-8")) > MAX_PAGE_BYTES:
        raise PageTooLong(f"the page has more than {MAX_PAGE_BYTES} bytes")
    return text


def page_for(topic: str, slug: str, data_root: Path | str, text: str) -> Page:
    """The page of one question. Raises `ValueError` for a path that is not safe."""
    path = layout.knowledge_page(topic, slug, data_root)
    rel = path.relative_to(layout.knowledge_root(data_root)).as_posix()
    return Page(path=path, rel=rel, text=text)


def write_page(page: Page, data_root: Path | str) -> Path:
    """Write a page under `wiki/knowledge/` and return its path."""
    return write_text(page.path, page.text, data_root)
