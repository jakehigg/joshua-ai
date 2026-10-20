"""The published knowledge pages of a topic: what the insights job compares with.

A page counts when its frontmatter has `status: published` and it is one of
these:

- a page that the dossier of the topic names for an `answered` question;
- a page in the folder of the topic under `wiki/knowledge/`
  (`layout.knowledge_page(topic, <slug>)`);
- a page that the caller gives as text, at a path in that folder. A research
  run gives the pages it publishes, so a dry run can compare with them before
  they are on the disk.

Each path is relative to `wiki/knowledge/`, as the dossier writes it.

Each page also gives its note for us: the text under the heading
`## Where this differs for us` that `jobs/research/write.py` writes, on one
line and cut at a sentence end to `NOTE_MAX_CHARS` or less. A page without
that section gives an empty note.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from joshua_shared import frontmatter, layout

from joshua_core.jobs.research.dossier import load_dossier
from joshua_core.jobs.research.write import STATUS_PUBLISHED
from joshua_core.jobs.topics import read_page

# The heading that `render_page` in `jobs/research/write.py` puts above the
# editor's local note. A test renders a page and reads the note back.
LOCAL_HEADING = "Where this differs for us"

# The longest note for us that the worker sees, in characters.
NOTE_MAX_CHARS = 600
_SENTENCE_ENDS = (". ", "! ", "? ", ".) ")


@dataclass(frozen=True)
class KnownPage:
    """One published knowledge page: its path relative to `wiki/knowledge/`,
    its whole text, its one-line short answer, and the editor's note for us."""

    rel: str
    text: str
    answer_short: str
    us_note: str = ""


def cut_at_sentence(text: str, limit: int = NOTE_MAX_CHARS) -> str:
    """`text` on one line, cut to `limit` characters or less. The cut is at the
    last sentence end before the limit, else at the last space."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    head = flat[: limit + 1]
    end = max(head.rfind(mark) for mark in _SENTENCE_ENDS)
    if end > 0:
        return head[: end + 1].rstrip()
    space = head.rfind(" ")
    return (head[:space] if space > 0 else flat[:limit]).rstrip()


def us_note(body: str, limit: int = NOTE_MAX_CHARS) -> str:
    """The text under `## Where this differs for us` in the body of a page, up
    to the next heading of level 2 or 1, cut by `cut_at_sentence`. Empty when
    the page has no such section."""
    lines = body.splitlines()
    heading = f"## {LOCAL_HEADING}"
    for at, line in enumerate(lines):
        if line.strip() == heading:
            kept: list[str] = []
            for rest in lines[at + 1 :]:
                if rest.startswith("## ") or rest.startswith("# "):
                    break
                kept.append(rest)
            return cut_at_sentence("\n".join(kept), limit)
    return ""


def _known(rel: str, text: str) -> KnownPage | None:
    meta, body = frontmatter.split(text)
    if meta.get("status") != STATUS_PUBLISHED:
        return None
    short = meta.get("answer_short")
    return KnownPage(rel, text, short if isinstance(short, str) else "", us_note(body))


def _safe_rel(rel: str, data_root: Path) -> Path | None:
    root = layout.knowledge_root(data_root)
    if not rel or rel.startswith("/") or "\\" in rel or ".." in rel.split("/"):
        return None
    path = root / rel
    if layout.is_hidden(path, root) or not rel.endswith(".md"):
        return None
    return path


def topic_folder_rel(topic_rel: str, data_root: Path | str) -> str:
    """The folder of the knowledge pages of a topic, relative to `wiki/knowledge/`."""
    folder = layout.knowledge_dossier(topic_rel, data_root).with_suffix("")
    return folder.relative_to(layout.knowledge_root(data_root)).as_posix()


def published_pages(
    topic_rel: str, data_root: Path | str, extra: Mapping[str, str] | None = None
) -> dict[str, KnownPage]:
    """The published knowledge pages of a topic, by path, sorted by path."""
    data_root = Path(data_root)
    found: dict[str, KnownPage] = {}
    dossier = load_dossier(topic_rel, data_root)
    if dossier is not None:
        for question in dossier.questions:
            if question.status != "answered" or not question.page:
                continue
            path = _safe_rel(question.page, data_root)
            if path is None or not path.is_file():
                continue
            page = _known(question.page, read_page(path))
            if page is not None:
                found[page.rel] = page
    folder_rel = topic_folder_rel(topic_rel, data_root)
    folder = layout.knowledge_root(data_root) / folder_rel
    if folder.is_dir():
        for path in sorted(folder.glob("*.md")):
            if path.name.startswith(".") or not path.is_file():
                continue
            rel = f"{folder_rel}/{path.name}"
            page = _known(rel, read_page(path))
            if page is not None:
                found[rel] = page
    for rel, text in (extra or {}).items():
        if rel.rsplit("/", 1)[0] != folder_rel or _safe_rel(rel, data_root) is None:
            continue
        page = _known(rel, text)
        if page is not None:
            found[rel] = page
    return dict(sorted(found.items()))


def normalize_page(value: str) -> str:
    """A `knowledge_says` path as the worker gave it, relative to
    `wiki/knowledge/`: a leading `/` and a leading `knowledge/` are removed."""
    text = value.strip().lstrip("/")
    prefix = f"{layout.KNOWLEDGE}/"
    return text[len(prefix) :] if text.startswith(prefix) else text
