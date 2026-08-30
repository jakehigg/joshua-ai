"""Source adapters — the modular RAG.

The indexer never reads files or APIs itself; it drives source adapters behind
one protocol. ``files`` is the kernel and is always present; every other adapter
(``memos``, ``wikijs``, ``karakeep``, …) is optional and lands with its own
ticket. Adding a platform is one module plus one ``memory.sources`` config
entry; the indexer does not change.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Document:
    """One document a source offers for indexing."""

    source: str  # adapter name: "files" | "memos" | …
    uri: str  # stable id within the source (files: the relative path)
    person_id: str | None  # None = shared/shared scope
    title: str
    text: str  # markdown
    updated_at: datetime
    rev: str  # opaque change marker (files: sha256; memos: updateTime)
    provenance: str = "own"  # "own" | "external"
    tags: tuple[str, ...] = ()
    doc_date: date | None = None  # for recency ranking (blog posts, memos)
    # Parsed frontmatter ``key: value`` pairs. ``FilesSource`` fills it; a flat
    # source (memos) leaves it empty. The indexer reads it for a taught skill.
    frontmatter: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class Source(Protocol):
    name: str

    def list_documents(self) -> AsyncIterator[Document]:
        """Full listing, cheap enough to run on a schedule."""
        ...

    async def fetch(self, uri: str) -> Document | None:
        """One document; None means it is gone and its rows should be purged."""
        ...
