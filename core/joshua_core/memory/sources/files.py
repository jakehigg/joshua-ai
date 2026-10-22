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

Three kinds of page are not memory, and the listing leaves them out
(:func:`not_memory`): a journal entry whose frontmatter holds ``run_id``, which
is the report of a job run, a journal entry whose frontmatter holds
``digest``, which a digest entry covers, and any page whose frontmatter holds
``index: false``, such as a dossier or the README list of a job folder. The
page stays on the disk. ``left_out`` names the pages the last listing left
out, so the indexer can report them, and the indexer purges their rows as it
does the rows of a deleted file.
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

_JOURNAL_PREFIX = "wiki/journal/"

_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")


def _frontmatter_lines(text: str) -> tuple[list[str], str] | None:
    """The lines of a leading ``---`` frontmatter block and the body, or None
    when the text has no block."""
    if not text.startswith("---"):
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i], "\n".join(lines[i + 1 :]).lstrip("\n")
    return None


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a leading ``---`` YAML frontmatter block from the body.

    Returns ``({}, text)`` when there is no frontmatter. Only simple
    ``key: value`` lines are read — enough for ``date``/``tags``/``provenance``.
    """
    found = _frontmatter_lines(text)
    if found is None:
        return {}, text
    lines, body = found
    meta: dict[str, str] = {}
    for line in lines:
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    return meta, body


def _top_level(text: str) -> dict[str, str]:
    """The top-level ``key: value`` pairs of the frontmatter block.

    An indented line belongs to the value of a key above it, such as one
    question in the list of a dossier, so it is not read.
    """
    found = _frontmatter_lines(text)
    if found is None:
        return {}
    meta: dict[str, str] = {}
    for line in found[0]:
        if line[:1].isspace() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip().lower()] = value.strip().strip("'\"")
    return meta


def not_memory(path: str, text: str) -> bool:
    """True when the page at the data-relative ``path`` is not memory.

    A journal entry whose frontmatter holds ``run_id`` is the report of a job
    run. A journal entry whose frontmatter holds ``digest`` is covered by the
    digest entry that the key names, and the index keeps the digest instead.
    A page whose frontmatter holds ``index: false`` asks to stay out of the
    index.
    """
    meta = _top_level(text)
    if meta.get("index", "").lower() == "false":
        return True
    return path.startswith(_JOURNAL_PREFIX) and bool(meta.get("run_id") or meta.get("digest"))


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
        self._left_out: list[str] = []

    def left_out(self) -> list[str]:
        """The paths of the pages the last listing left out as not memory."""
        return list(self._left_out)

    def _markdown(self, base: Path) -> Iterator[Path]:
        if not base.is_dir():
            return
        for file in sorted(base.rglob("*.md")):
            if not file.is_file() or is_hidden(file, base):
                continue
            yield file

    def _document(self, file: Path, *, path: str) -> Document | None:
        """The document of a page, or None when the page is not memory."""
        raw = file.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        if not_memory(path, text):
            return None
        rev = hashlib.sha256(raw).hexdigest()
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
        left_out: list[str] = []
        for tree in _TREES:
            for file in self._markdown(self._data_dir / tree):
                path = str(file.relative_to(self._data_dir))
                document = self._document(file, path=path)
                if document is None:
                    left_out.append(path)
                    continue
                yield document
        self._left_out = left_out
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
