"""Plain dataclass mirroring a ``kb_chunk`` row.

Dependency-light (stdlib only) so this imports without psycopg — the offline
chunker/ranking tests depend on that.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class KbChunk:
    """One indexed slice of a document, scoped by person.

    ``person_id`` is None for a shared document. ``similarity`` is
    populated only on vector-search reads (cosine, 0..1); it is not a stored
    column.
    """

    id: int = 0
    person_id: str | None = None
    source: str = ""  # adapter name: 'files' | 'memos' | ...
    kind: str = ""  # adapter sub-type: files → 'blog' | 'wiki' | 'shared'
    path: str = ""  # Document.uri, e.g. 'wiki/recipes/pizza.md'
    provenance: str = "own"  # 'own' | 'external'
    title: str = ""
    heading: str = ""
    chunk_index: int = 0
    text: str = ""
    similarity: float | None = None
    file_mtime: datetime | None = None
    file_sha256: str = ""
    doc_date: date | None = None  # recency ranking (blog posts)
    indexed_at: datetime | None = None
