"""The ``documents`` source adapter: a pointer to each document, not its body.

A document is what Joshua is working on or has produced: a plan, a report, a
spec. It lives at ``documents/<owner>/<slug>.md``, where ``<owner>`` is a
member id or ``shared``. This adapter yields one :class:`Document` per
document, and the indexer writes one row for it (kind ``document``):

- ``title`` is the frontmatter ``title``, else the first ``# `` heading, else
  the slug;
- ``text`` is the frontmatter ``summary``, else the first paragraph of the
  body, cut to ``SUMMARY_CHARS``;
- the kind and the status go in the row heading, as ``<kind>, <status>``.

The rest of the body is never read into the index. A document with
``status: archived``, a document with ``format: html``, and every ``.html``
file are not indexed at all. A folder that names no member and is not
``shared`` is ignored, and so is a dot entry (``.trash/``, ``.git/``) at any
depth. A volume with no ``documents/`` folder yields nothing.

``person_id`` is the owner, or None for ``shared``. A search finds a row of an
owner only for that owner (``MemoryStore.kb_search``), and recall shows a
document only in a conversation of a member (``memory.search.document_reader``).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator, Collection, Iterator
from datetime import UTC, datetime
from pathlib import Path

from joshua_shared import layout
from joshua_shared.layout import is_hidden

from joshua_core.memory.models import KbChunk
from joshua_core.memory.sources import Document
from joshua_core.memory.sources.files import _first_heading, _parse_tags, _split_frontmatter

DOCUMENTS_SOURCE = "documents"
# The path prefix of every row of this source, relative to the data volume.
DOCUMENTS_PREFIX = "documents/"
SHARED_OWNER = "shared"
# The summary of a document holds at most this many characters.
SUMMARY_CHARS = 500

DOCUMENT_KINDS = ("plan", "report", "spec", "artifact", "note")
DOCUMENT_STATUSES = ("draft", "active", "final", "archived")
DEFAULT_KIND = "note"
DEFAULT_STATUS = "draft"

_HEADING_LINE = re.compile(r"^#{1,6}\s")


def _value(meta: dict[str, str], key: str) -> str:
    """A frontmatter value with its quotes removed."""
    value = meta.get(key, "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value


def _choice(value: str, allowed: tuple[str, ...], default: str) -> str:
    value = value.lower()
    return value if value in allowed else default


def first_paragraph(body: str) -> str:
    """The first paragraph of ``body`` that is not a heading, on one line."""
    for block in re.split(r"\n\s*\n", body):
        lines = [line for line in block.splitlines() if not _HEADING_LINE.match(line)]
        text = " ".join(" ".join(lines).split())
        if text:
            return text
    return ""


def _cut(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= SUMMARY_CHARS:
        return text
    return text[:SUMMARY_CHARS].rstrip() + "…"


def pointer_heading(kind: str, status: str) -> str:
    """The row heading of a document: ``<kind>, <status>``."""
    return f"{kind}, {status}"


def document_pointer(chunk: KbChunk) -> str:
    """``document: <title> (<path>, <kind>, <status>)``, the only form in which
    recall shows a document. The body is read with ``read_document``."""
    title = chunk.title or chunk.path
    detail = f"{chunk.path}, {chunk.heading}" if chunk.heading else chunk.path
    return f"document: {title} ({detail})"


def viewer_path(path: str) -> str | None:
    """``/documents/<owner>/<slug>`` for ``documents/<owner>/<slug>.md``, else
    None."""
    if not path.startswith(DOCUMENTS_PREFIX) or not path.endswith(".md"):
        return None
    rest = path[len(DOCUMENTS_PREFIX) : -len(".md")]
    parts = rest.split("/")
    if len(parts) < 2 or any(part in ("", ".", "..") for part in parts):
        return None
    return "/documents/" + rest


class DocumentsSource:
    """Indexes the title, kind, status, and summary of each document."""

    name = DOCUMENTS_SOURCE

    def __init__(self, data_dir: Path | str, *, members: Collection[str]):
        self._data_dir = Path(data_dir)
        self._root = layout.documents_root(self._data_dir)
        self._owners = {*members, SHARED_OWNER}

    def _files(self) -> Iterator[tuple[str, Path]]:
        if not self._root.is_dir():
            return
        for folder in sorted(self._root.iterdir()):
            if not folder.is_dir() or folder.name not in self._owners:
                continue
            for file in sorted(folder.rglob("*.md")):
                if file.is_file() and not is_hidden(file, self._root):
                    yield folder.name, file

    def _document(self, owner: str, file: Path) -> Document | None:
        """The document of ``file``, or None when it is not indexed."""
        raw = file.read_bytes()
        meta, body = _split_frontmatter(raw.decode("utf-8", errors="replace"))
        status = _choice(_value(meta, "status"), DOCUMENT_STATUSES, DEFAULT_STATUS)
        if status == "archived" or _value(meta, "format").lower() == "html":
            return None
        kind = _choice(_value(meta, "kind"), DOCUMENT_KINDS, DEFAULT_KIND)
        title = _value(meta, "title") or _first_heading(body) or file.stem
        summary = _cut(_value(meta, "summary") or first_paragraph(body))
        return Document(
            source=self.name,
            uri=file.relative_to(self._data_dir).as_posix(),
            person_id=None if owner == SHARED_OWNER else owner,
            title=title,
            text=summary,
            updated_at=datetime.fromtimestamp(file.stat().st_mtime, tz=UTC),
            rev=hashlib.sha256(raw).hexdigest(),
            tags=_parse_tags(meta.get("tags")),
            frontmatter={"kind": kind, "status": status},
        )

    async def list_documents(self) -> AsyncIterator[Document]:
        for owner, file in self._files():
            document = self._document(owner, file)
            if document is not None:
                yield document

    async def fetch(self, uri: str) -> Document | None:
        """One document by its data-relative path, or None."""
        if not uri.startswith(DOCUMENTS_PREFIX) or not uri.endswith(".md"):
            return None
        parts = uri.split("/")
        if len(parts) < 3 or any(part in ("", ".", "..") for part in parts):
            return None
        owner = parts[1]
        file = self._data_dir / uri
        if owner not in self._owners or not file.is_file() or is_hidden(file, self._root):
            return None
        return self._document(owner, file)
