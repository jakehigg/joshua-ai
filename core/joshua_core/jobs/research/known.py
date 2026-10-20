"""Knowledge that we already hold: the published knowledge pages near a question.

`find_known` embeds a general question, searches the `kind: knowledge`
chunks of the index at `min_sim` or more, and gives the published knowledge
pages of the hits, of any topic, the closest first, at most `limit`. A page
counts only when it is under `wiki/knowledge/`, is not hidden, has a
`question` in its frontmatter, and has `status: published`. A dossier has no
`question`, so it never counts.

The gate (`gate.py`) reads the same pages to find a duplicate, and the crew
(`crew.py`) gives them to the `apply` worker before any lookup.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from joshua_shared import frontmatter, layout
from joshua_shared.log import get_logger

from joshua_core.jobs.bundle import BundleStore
from joshua_core.jobs.topics import read_page

logger = get_logger("jobs.research.known")

STATUS_PUBLISHED = "published"
KNOWLEDGE_KIND = "knowledge"
# One page that the `apply` worker reads is cut to this many characters.
PAGE_CHARS = 12000
_INDEX_WIKI_PREFIX = "wiki/"
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}

# Embeds one text and gives its vectors. A test passes its own.
Embedder = Callable[[str], Awaitable[list[list[float]]]]


def answer_page(index_path: str, data_root: Path) -> tuple[str, dict[str, Any]] | None:
    """The wiki-relative path and the frontmatter of a knowledge page with a
    `question`, or None. `index_path` is in the index form (`wiki/...`)."""
    if not index_path.startswith(_INDEX_WIKI_PREFIX):
        return None
    rel = index_path[len(_INDEX_WIKI_PREFIX) :]
    wiki = layout.wiki_root(data_root)
    path = wiki / rel
    if ".." in rel.split("/") or not rel.startswith(f"{layout.KNOWLEDGE}/"):
        return None
    if layout.is_hidden(path, wiki) or not path.is_file():
        return None
    meta, _ = frontmatter.split(read_page(path))
    return (rel, meta) if isinstance(meta.get("question"), str) else None


@dataclass(frozen=True)
class KnownPage:
    """A published knowledge page: its path relative to the wiki root, its
    topic, its question, its short answer, its confidence, its sources as the
    frontmatter holds them, and its whole text."""

    path: str
    topic: str
    question: str
    answer_short: str
    confidence: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""


def known_page(index_path: str, data_root: Path) -> KnownPage | None:
    """The published knowledge page of an index path, or None."""
    found = answer_page(index_path, data_root)
    if found is None:
        return None
    rel, meta = found
    if meta.get("status") != STATUS_PUBLISHED:
        return None
    sources = [s for s in meta.get("sources") or [] if isinstance(s, dict) and s.get("url")]
    text = read_page(layout.wiki_root(data_root) / rel)
    return KnownPage(
        path=rel,
        topic=str(meta.get("topic") or ""),
        question=str(meta["question"]),
        answer_short=str(meta.get("answer_short") or ""),
        confidence=str(meta.get("confidence") or "medium"),
        sources=sources,
        text=text[:PAGE_CHARS],
    )


def lowest_confidence(pages: Sequence[KnownPage]) -> str:
    """The lowest confidence of `pages`: `low`, `medium`, or `high`. An
    unknown value counts as `low`."""
    if not pages:
        return "low"
    return min(
        (p.confidence if p.confidence in _CONFIDENCE_RANK else "low" for p in pages),
        key=lambda c: _CONFIDENCE_RANK[c],
    )


@dataclass
class KnowledgeSearch:
    """What the crew needs to find the knowledge we hold: the index, the
    embedder, the data root, the cosine floor (`jobs.research.apply_sim`), and
    the most pages (`jobs.research.apply_pages`)."""

    store: BundleStore
    embed: Embedder
    data_root: Path
    min_sim: float
    limit: int
    notes: list[str] = field(default_factory=list)


async def find_known(question: str, search: KnowledgeSearch) -> list[KnownPage]:
    """The published knowledge pages near `question`, of any topic, the
    closest first, at most `search.limit`. A failure of the embedder or of
    the index gives `[]` and a note on `search.notes`."""
    try:
        vectors = await search.embed(question)
    except RuntimeError as exc:
        search.notes.append("The search for knowledge we hold did not run: no embedding model.")
        logger.warning({"message": "research known search has no vectors", "error": str(exc)})
        return []
    if not vectors:
        return []
    try:
        near = await search.store.kb_near_paths(
            vectors, kinds=[KNOWLEDGE_KIND], min_sim=search.min_sim
        )
    except Exception as exc:  # noqa: BLE001 — no index means no known page
        search.notes.append("The search for knowledge we hold did not run.")
        logger.warning({"message": "research known search failed", "error": str(exc)})
        return []
    pages: list[KnownPage] = []
    for index_path, sim in sorted(near.items(), key=lambda item: (-item[1], item[0])):
        if sim < search.min_sim:
            continue
        page = known_page(index_path, search.data_root)
        if page is not None:
            pages.append(page)
        if len(pages) >= search.limit:
            break
    return pages
