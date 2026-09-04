"""The ``files`` source adapter — the kernel RAG source.

Walks the wiki and yields one :class:`Document` per markdown page, all shared
scope (``person_id`` None): Joshua's memory is one wiki now, not per-person
content.

- ``wiki/journal/**.md``  → the day-by-day pages and entries.
- ``wiki/people/*.md``    → Joshua's profile of a person (and the shared
  profile, ``everyone.md``).
- anything else under ``wiki/`` → an ordinary wiki page.
- ``shared/**.md``, if anything is still there, is indexed too, for
  compatibility with a volume ``migrate_to_one_wiki`` has not yet run on.

``rev`` is the file sha256. ``title`` is the first ``# `` heading or the file
name. ``doc_date`` is the journal day a path belongs to
(``layout.journal_day_from_path``), else a frontmatter ``date``, else None.
Frontmatter ``date``/``tags``/``provenance`` are honored when present. A dot
entry (``.trash/``, ``.git/``, and so on, at any depth) and a ``*.meta.json``
sidecar are ignored.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
from pathlib import Path

from joshua_shared import layout
from joshua_shared.layout import is_hidden
from joshua_shared.log import get_logger

from joshua_core.memory.sources import Document

logger = get_logger("memory.sources.files")

# Top-level trees this adapter walks. ``shared`` is compatibility only: after
# ``migrate_to_one_wiki`` it holds no markdown, but an older volume, or a hand
# copy, may still have some.
_TREES = ("wiki", "shared")

_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a leading ``---`` YAML frontmatter block from the body.

    Returns ``({}, text)`` when there is no frontmatter. Only simple
    ``key: value`` lines are read — enough for ``date``/``tags``/``provenance``.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            meta: dict[str, str] = {}
            for line in lines[1:i]:
                if ":" in line:
                    key, _, value = line.partition(":")
                    meta[key.strip().lower()] = value.strip()
            body = "\n".join(lines[i + 1 :]).lstrip("\n")
            return meta, body
    return {}, text


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        m = _H1.match(line)
        if m:
            return m.group(1).strip()
    return ""


def _parse_tags(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    value = value.strip().strip("[]")
    return tuple(t.strip().strip("'\"") for t in value.split(",") if t.strip())


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


class FilesSource:
    """Indexes the wiki's markdown. Always present (the kernel)."""

    name = "files"

    def __init__(self, data_dir: Path | str):
        self._data_dir = Path(data_dir)

    def _markdown(self, base: Path) -> Iterator[Path]:
        if not base.is_dir():
            return
        for file in sorted(base.rglob("*.md")):
            if not file.is_file() or is_hidden(file, base):
                continue
            yield file

    def _document(self, file: Path, *, path: str) -> Document:
        raw = file.read_bytes()
        rev = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8", errors="replace")
        meta, body = _split_frontmatter(text)
        mtime = datetime.fromtimestamp(file.stat().st_mtime, tz=UTC)
        title = _first_heading(body) or file.stem
        provenance = "external" if meta.get("provenance", "").lower() == "external" else "own"
        doc_date = layout.journal_day_from_path(file, self._data_dir) or _parse_date(
            meta.get("date")
        )
        return Document(
            source=self.name,
            uri=path,
            person_id=None,
            title=title,
            text=body,
            updated_at=mtime,
            rev=rev,
            provenance=provenance,
            tags=_parse_tags(meta.get("tags")),
            doc_date=doc_date,
            frontmatter=meta,
        )

    async def list_documents(self) -> AsyncIterator[Document]:
        for tree in _TREES:
            for file in self._markdown(self._data_dir / tree):
                path = str(file.relative_to(self._data_dir))
                yield self._document(file, path=path)

    async def fetch(self, uri: str) -> Document | None:
        """Resolve one document by its relative path. Cold path — the indexer
        uses ``list_documents``; this backs targeted refresh."""
        if not uri.startswith(tuple(f"{tree}/" for tree in _TREES)):
            return None
        file = self._data_dir / uri
        if file.is_file() and not is_hidden(file, self._data_dir):
            return self._document(file, path=uri)
        return None
