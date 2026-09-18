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

It also yields one document per attachment that carries text, from the
metadata file beside it: the description, and the words that were read out of
the file. The file itself is never read here. That text came from outside, so
the document is ``external``, and a search reaches the page of a PDF or the
total on a receipt. ``attachments.extract.embed: false`` turns it off.

``rev`` is the file sha256. ``title`` is the first ``# `` heading or the file
name. ``doc_date`` is the journal day a path belongs to
(``layout.journal_day_from_path``), else a frontmatter ``date``, else None.
Frontmatter ``date``/``tags``/``provenance`` are honored when present. A dot
entry (``.trash/``, ``.git/``, and so on, at any depth) and a ``*.meta.json``
metadata file are ignored.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
from pathlib import Path

from joshua_shared import layout
from joshua_shared.attachments import is_meta, meta_path, read_meta
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
    """Indexes the wiki's markdown and the text of an attachment. The kernel source."""

    name = "files"

    def __init__(self, data_dir: Path | str, *, index_attachments: bool = True):
        self._data_dir = Path(data_dir)
        self._index_attachments = index_attachments

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

    def _attachments(self) -> Iterator[Path]:
        """Every stored attachment that carries text, in each attachment tree."""
        for base in layout.attachment_roots(self._data_dir):
            for file in sorted(base.rglob("*")):
                if not file.is_file() or is_meta(file) or is_hidden(file, base):
                    continue
                yield file

    def _attachment_document(self, file: Path, *, path: str) -> Document | None:
        """One document from the metadata file of an attachment, or None.

        The text and the description were written by a reader of the file, not
        by a person here, so the document is ``external``. ``rev`` is the digest
        of the metadata file, so a new description re-indexes the document and
        nothing else does.
        """
        meta = read_meta(file)
        if meta is None or not meta.has_text():
            return None
        raw = meta_path(file).read_bytes()
        description = meta.description
        title = description.subject if description is not None else file.stem
        header = f"{description.match_text()}\n\n" if description is not None else ""
        name = meta.original_name or file.name
        return Document(
            source=self.name,
            uri=path,
            person_id=None,
            title=title,
            text=f"{header}The file {name} says:\n\n{meta.extracted_text or ''}",
            updated_at=datetime.fromtimestamp(file.stat().st_mtime, tz=UTC),
            rev=hashlib.sha256(raw).hexdigest(),
            provenance="external",
            tags=(description.kind,) if description is not None else (),
            doc_date=_parse_date(meta.received_at),
        )

    async def list_documents(self) -> AsyncIterator[Document]:
        for tree in _TREES:
            for file in self._markdown(self._data_dir / tree):
                path = str(file.relative_to(self._data_dir))
                yield self._document(file, path=path)
        if not self._index_attachments:
            return
        for file in self._attachments():
            document = self._attachment_document(file, path=str(file.relative_to(self._data_dir)))
            if document is not None:
                yield document

    async def fetch(self, uri: str) -> Document | None:
        """Resolve one document by its relative path. Cold path — the indexer
        uses ``list_documents``; this backs targeted refresh."""
        if not uri.startswith(tuple(f"{tree}/" for tree in _TREES)) and (
            layout.attachment_area(uri) is None
        ):
            return None
        file = self._data_dir / uri
        if not file.is_file() or is_hidden(file, self._data_dir):
            return None
        if layout.attachment_area(uri) is not None:
            if not self._index_attachments:
                return None
            return self._attachment_document(file, path=uri)
        return self._document(file, path=uri)
