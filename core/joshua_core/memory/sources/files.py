"""The ``files`` source adapter — the kernel RAG source.

Walks the data volume and yields one :class:`Document` per markdown file:

- ``people/<id>/blog/**.md`` → kind ``blog``, scoped to that person
- ``wiki/**.md``              → kind ``wiki``, shared scope (``person_id`` None)
- ``shared/**.md``            → kind ``shared``, shared scope (``person_id`` None)

``rev`` is the file sha256. ``title`` is the first ``# `` heading or the file
name. ``doc_date`` comes from a ``blog/YYYY-MM-DD*.md`` name (the nightly digest
``YYYY-MM-DD.md`` and agent posts ``YYYY-MM-DD-HHMM-<slug>.md``) or a frontmatter
``date``. Frontmatter ``date``/``tags``/``provenance`` are honored when present.
A dot entry (``.trash/``, ``.git/``, and so on, at any depth) and a
``*.meta.json`` sidecar are ignored.

A directory below ``people/`` that names nobody holds no document that can be
indexed, because a chunk carries a foreign key to ``people``. The adapter takes
the roster and skips such a directory, and says so one time per pass. A restore
from an older volume, a hand copy, and a partial migration all make this shape.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Iterator
from datetime import UTC, date, datetime
from pathlib import Path

from joshua_shared import layout
from joshua_shared.layout import is_hidden
from joshua_shared.log import get_logger

from joshua_core.memory.sources import Document

logger = get_logger("memory.sources.files")

# Person kinds this adapter indexes. ``attachments/`` and ``profile.md`` are not
# retrieval content and are skipped.
_PERSON_KINDS = ("blog",)

# Top-level trees that everyone can see.
_SHARED_TREES = ("wiki", "shared")

_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_BLOG_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


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


def _date_from_blog_name(path: str) -> date | None:
    if not path.startswith("blog/"):
        return None
    m = _BLOG_DATE.match(Path(path).name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


class FilesSource:
    """Indexes markdown under the data volume. Always present (the kernel)."""

    name = "files"

    def __init__(
        self,
        data_dir: Path | str,
        *,
        persons: Callable[[], Awaitable[Collection[str]]] | None = None,
    ):
        self._data_dir = Path(data_dir)
        self._persons = persons

    async def _person_ids(self) -> list[str]:
        """The person directories to walk, and a line for each one skipped.

        ``persons`` is the roster. Without it every slug directory is walked,
        which is what a caller with no database wants. With it, a directory
        that names nobody is left alone: its documents cannot be stored, and
        offering them makes the same failure on every pass.
        """
        people = self._data_dir / "people"
        if not people.is_dir():
            return []
        known = set(await self._persons()) if self._persons is not None else None
        ids: list[str] = []
        orphans: list[str] = []
        for entry in sorted(people.iterdir()):
            if not entry.is_dir():
                continue
            try:
                person_id = layout.safe_segment(entry.name)
            except ValueError:
                orphans.append(entry.name)  # a stray non-slug directory is not a person
                continue
            if known is not None and person_id not in known:
                orphans.append(entry.name)
                continue
            ids.append(person_id)
        if orphans:
            # One line for the pass. One line per document is how a single
            # stray directory made thousands of identical lines a day.
            logger.warning(
                {
                    "message": "kb skipped directories that name no person",
                    "count": len(orphans),
                    "directories": sorted(orphans)[:10],
                }
            )
        return ids

    def _markdown(self, base: Path) -> Iterator[Path]:
        if not base.is_dir():
            return
        for file in sorted(base.rglob("*.md")):
            if not file.is_file() or is_hidden(file, base):
                continue
            yield file

    def _document(self, file: Path, *, person_id: str | None, path: str) -> Document:
        raw = file.read_bytes()
        rev = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8", errors="replace")
        meta, body = _split_frontmatter(text)
        mtime = datetime.fromtimestamp(file.stat().st_mtime, tz=UTC)
        title = _first_heading(body) or file.stem
        provenance = "external" if meta.get("provenance", "").lower() == "external" else "own"
        doc_date = _parse_date(meta.get("date")) or _date_from_blog_name(path)
        return Document(
            source=self.name,
            uri=path,
            person_id=person_id,
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
        for person_id in await self._person_ids():
            root = layout.person_root(person_id, self._data_dir)
            for kind in _PERSON_KINDS:
                base = root / kind
                for file in self._markdown(base):
                    path = str(file.relative_to(root))
                    yield self._document(file, person_id=person_id, path=path)
        for tree in _SHARED_TREES:
            for file in self._markdown(self._data_dir / tree):
                path = str(file.relative_to(self._data_dir))
                yield self._document(file, person_id=None, path=path)

    async def fetch(self, uri: str) -> Document | None:
        """Resolve one document by its relative path. Cold path — the indexer
        uses ``list_documents``; this backs targeted refresh."""
        if uri.startswith(tuple(f"{tree}/" for tree in _SHARED_TREES)):
            file = self._data_dir / uri
            if file.is_file() and not is_hidden(file, self._data_dir):
                return self._document(file, person_id=None, path=uri)
            return None
        if not uri.startswith(tuple(f"{kind}/" for kind in _PERSON_KINDS)):
            return None
        for person_id in await self._person_ids():
            root = layout.person_root(person_id, self._data_dir)
            file = root / uri
            if file.is_file() and not is_hidden(file, root):
                return self._document(file, person_id=person_id, path=uri)
        return None
