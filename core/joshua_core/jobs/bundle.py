"""The memory bundle: everything Joshua holds about one topic, in one text.

Every job reads a topic through its bundle, so the curator, the researcher, and
the insights job see the same pages. `build_bundle` puts the parts in this
order:

1. the topic page;
2. the pages it links to inside its own section, one hop, in link order;
3. the snapshot block: one part `snapshots/<name>` per snapshot that has an
   item close to the topic page (see below);
4. the journal entries about it, newest first: each entry that names the
   topic, and the entries with a chunk at or above `jobs.bundle.sim` to a
   chunk of the topic page. The entries that only the similarity brings in
   are capped at `jobs.bundle.max_vector_entries`, the closest first;
5. the dossier of the topic and its knowledge pages, when they exist;
6. the answered questions of the topic: a page in `wiki/questions/` whose
   frontmatter `topic` is this topic and whose `status` is `answered`, as a
   short block of the question and the answer only. An open or a dismissed
   question is never in a bundle;
7. the open insights of the topic: an insight page whose frontmatter `topic`
   is this topic and whose `status` is `new`, `told`, or `acknowledged`.

Each part starts with a line `## <path>`, the path relative to the wiki root,
so a worker can cite it. The text stops at `jobs.bundle.max_chars`: a part that
does not fit is left out with every part after it, and `truncated` is true.
The topic page is never cut.

The snapshot block holds the items of live state (`memory.snapshots`, the
index rows of kind `snapshot`) that are close to the topic page. A job may
see a snapshot whose scope is `shared`, and a snapshot of one person only
when its entry sets `jobs: true`, because a bundle feeds wiki pages that every
reader of the wiki can open. The chunks of the topic page are the query. An
item comes in when it is at `jobs.bundle.snapshot_sim` or more and either
names the topic (a title or an alias, by the journal rule) or is at
`jobs.bundle.snapshot_strong_sim` or more. Items come the closest first, at
most `jobs.bundle.snapshot_items` items, and the block stops at the first
item that does not fit in `jobs.bundle.snapshot_chars` characters. A part
starts with the line `Live snapshot of <server> <tool> as of <local time>.`,
the time of the latest good fetch, and then holds one line per item, at most
`SNAPSHOT_LINE_CHARS` characters. A snapshot with no good fetch is left out.
With no embedding model there is no block, and `Bundle.notes` says so.
`Bundle.snapshots` records each part, its items, and their similarity.

The aliases of a topic are its frontmatter `aliases` list and the terms that
`vocabulary.md` at the wiki root gives it. A line of `vocabulary.md` that
links to the topic page names one term: the text before the first colon on
that line, with list marks, emphasis, code marks, and link syntax removed. A
line with no colon names no term.

A name or an alias that holds a person's name by the roster rule of
`topics` is not a name of the topic (`topics.topic_names`,
`topics.drop_people`), so a journal entry that only names a person is not
about the topic, and `Bundle.aliases` holds no bare name of a person. A
possessive title gives its generic name: `Alex's Caldo Verde` gives `Caldo
Verde`.

A journal entry names the topic when it holds a name of the topic by the rule
of `topics.names_pattern`. A name from the title or the file stem is a `title`
match, and an alias is an `alias` match. `Bundle.journal_via` says, for each
journal entry, how it got in: `title`, `alias`, or `vector <sim>`.

These journal entries are left out of every bundle, so a report never feeds
the next bundle:

- an entry that a job run wrote: its frontmatter has `run_id`, or its
  `source` is `curator`, `research`, or `insights`;
- an entry that a digest covers: its frontmatter has `digest`. The digest
  entry itself is in a bundle like any other entry;
- an entry whose file stem a glob in `jobs.bundle.exclude_slugs` matches
  (`fnmatch`, case-sensitive);
- an entry dated after the day of `now`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from joshua_shared import frontmatter, layout
from joshua_shared.config import JoshuaConfig, Person, Snapshot
from joshua_shared.log import get_logger

from joshua_core.jobs.topics import (
    RESERVED_FOLDERS,
    Topic,
    drop_people,
    names_pattern,
    read_page,
    topic_names,
)
from joshua_core.memory.indexer import embed_document
from joshua_core.memory.models import KbChunk
from joshua_core.memory.sources.snapshots import as_of, item_body, shown

logger = get_logger("jobs.bundle")

VOCABULARY_PAGE = "vocabulary.md"
OPEN_INSIGHT_STATUSES = ("new", "told", "acknowledged")
ANSWERED_QUESTION = "answered"
JOURNAL_KIND = "journal"
# The `source` of a journal entry that a job wrote.
JOB_SOURCES = ("curator", "research", "insights")
# The frontmatter key of a journal entry that a digest covers.
COVERED_KEY = "digest"
VIA_TITLE = "title"
VIA_ALIAS = "alias"
VIA_VECTOR = "vector"
# The index stores a files path relative to the data root.
_INDEX_WIKI_PREFIX = "wiki/"
# The folder of a snapshot part in a bundle: `snapshots/<name>`.
SNAPSHOTS_FOLDER = "snapshots"
# A snapshot line of a bundle holds at most this many characters of the item.
SNAPSHOT_LINE_CHARS = 300
# The snapshot query asks for this many times the item cap, so that the
# rows that the scope rule or the fetch time drops leave enough items.
_SNAPSHOT_QUERY_FACTOR = 4
NO_SNAPSHOT_MODEL = "No snapshot came into the bundle: no embedding model."
NO_SNAPSHOT_SEARCH = "The snapshot search did not run."

# `[text](target)`, not an image. The target stops at a space or `)`.
_MD_LINK_RE = re.compile(r"(?<!!)\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)(?:\s+[^)]*)?\)")
# `[[target]]` or `[[target|label]]`.
_WIKILINK_RE = re.compile(r"\[\[(?P<target>[^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_LIST_MARK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


class BundleStore(Protocol):
    """The index queries the bundle needs. `MemoryStore` implements them.
    The two snapshot queries run only when a job may see a snapshot."""

    async def kb_near_paths(
        self,
        embeddings: Sequence[list[float]],
        *,
        kinds: Sequence[str],
        min_sim: float,
        source: str = "files",
    ) -> dict[str, float]: ...

    async def kb_near_snapshots(
        self,
        embeddings: Sequence[list[float]],
        *,
        names: Sequence[str],
        min_sim: float,
        limit: int,
    ) -> list[KbChunk]: ...

    async def snapshot_times(self, names: Sequence[str]) -> dict[str, datetime]: ...


@dataclass(frozen=True)
class SnapshotPart:
    """One snapshot part of a bundle: the snapshot name, the part path
    `snapshots/<name>`, the text, and `{item uri: similarity}` of its items,
    closest first."""

    name: str
    path: str
    text: str
    items: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Bundle:
    """The bundle of one topic.

    `paths` lists every page in `text`, relative to the wiki root, in the order
    of the text. `chars` is `len(text)`. `truncated` is true when a part was
    left out because of `jobs.bundle.max_chars`. `journal_via` maps each
    journal entry in `paths` to how it got in: `title`, `alias`, or
    `vector <sim>`. `snapshots` holds each snapshot part in `paths`, with
    its items and their similarity. `notes` says why a step did not run.
    """

    topic: Topic
    paths: list[str]
    text: str
    chars: int
    truncated: bool
    aliases: list[str] = field(default_factory=list)
    journal_via: dict[str, str] = field(default_factory=dict)
    snapshots: list[SnapshotPart] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def snapshot_via(self) -> dict[str, float]:
        """`{item uri: similarity}` of every snapshot item in the bundle."""
        return {uri: sim for part in self.snapshots for uri, sim in part.items.items()}


@dataclass(frozen=True)
class _Part:
    path: str
    text: str

    def render(self) -> str:
        return f"## {self.path}\n\n{self.text.strip()}\n"


# -- links ------------------------------------------------------------------


def _link_targets(text: str) -> list[str]:
    """Every link target in `text`, in order: Markdown links, then wikilinks."""
    found: list[tuple[int, str, bool]] = []
    for match in _MD_LINK_RE.finditer(text):
        found.append((match.start(), match.group("target"), False))
    for match in _WIKILINK_RE.finditer(text):
        found.append((match.start(), match.group("target"), True))
    found.sort(key=lambda item: item[0])
    return [("[[" if wiki else "") + target for _, target, wiki in found]


def _normalize(parts: Sequence[str]) -> str | None:
    """Join path parts and resolve `.` and `..`, or None when it leaves the wiki."""
    out: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not out:
                return None
            out.pop()
            continue
        out.append(part)
    return "/".join(out) if out else None


def _resolve_link(target: str, page_rel: str, wiki_root: Path) -> str | None:
    """The wiki-relative path of the page a link names, or None.

    A Markdown link is relative to the folder of the page, or to the wiki root
    when it starts with `/`. A wikilink is tried relative to the folder of the
    page, then to the wiki root. `.md` is added when it is missing. The page
    must be a file on the disk.
    """
    wikilink = target.startswith("[[")
    raw = target[2:].strip() if wikilink else target
    if not wikilink and _SCHEME_RE.match(raw):
        return None
    raw = unquote(raw.split("#", 1)[0].split("?", 1)[0]).strip()
    if not raw or "\\" in raw:
        return None
    folder = list(PurePosixPath(page_rel).parent.parts)
    bases = [folder, []] if wikilink else ([[]] if raw.startswith("/") else [folder])
    for base in bases:
        rel = _normalize([*base, *raw.split("/")])
        if rel is None:
            continue
        if not rel.endswith(".md"):
            rel += ".md"
        absolute = wiki_root / rel
        if not layout.is_hidden(absolute, wiki_root) and absolute.is_file():
            return rel
    return None


def _section_of(rel: str) -> str:
    parts = PurePosixPath(rel).parts
    return parts[0] if len(parts) > 1 else ""


def _linked_pages(topic: Topic, text: str, wiki_root: Path) -> list[str]:
    """The pages the topic links to inside its own section, once each, in order."""
    out: list[str] = []
    for target in _link_targets(text):
        rel = _resolve_link(target, topic.path, wiki_root)
        if rel is None or rel == topic.path or rel in out:
            continue
        if _section_of(rel) != topic.section:
            continue
        if PurePosixPath(rel).parts[0] in RESERVED_FOLDERS:
            continue
        out.append(rel)
    return out


# -- aliases ----------------------------------------------------------------


def _clean_term(raw: str) -> str:
    text = _LIST_MARK_RE.sub("", raw)
    text = _MD_LINK_RE.sub(lambda m: m.group("text"), text)
    text = _WIKILINK_RE.sub(lambda m: m.group("target"), text)
    text = re.sub(r"[*_`]", "", text)
    return " ".join(text.split()).strip()


def vocabulary_terms(topic: Topic, wiki_root: Path) -> list[str]:
    """The terms that `vocabulary.md` gives the topic. See the module docstring."""
    text = read_page(wiki_root / VOCABULARY_PAGE)
    _, body = frontmatter.split(text)
    terms: list[str] = []
    for line in body.splitlines():
        if ":" not in line:
            continue
        targets = _link_targets(line)
        if not any(_resolve_link(t, VOCABULARY_PAGE, wiki_root) == topic.path for t in targets):
            continue
        term = _clean_term(line.split(":", 1)[0])
        # A colon inside a link target (a URL) is not the term separator.
        if term and "](" not in term:
            terms.append(term)
    return terms


def topic_aliases(topic: Topic, meta: dict[str, Any], wiki_root: Path) -> list[str]:
    """The frontmatter `aliases` of the topic and its vocabulary terms, once each."""
    raw = meta.get("aliases")
    if isinstance(raw, str):
        raw = [raw]
    found = [str(item).strip() for item in raw or [] if str(item).strip()]
    found.extend(vocabulary_terms(topic, wiki_root))
    out: list[str] = []
    seen: set[str] = set()
    for term in found:
        key = term.casefold()
        if key not in seen and key != topic.title.casefold():
            seen.add(key)
            out.append(term)
    return out


def topic_pattern(
    topic: Topic, wiki_root: Path, *, people: Sequence[Person] | None = None
) -> re.Pattern[str] | None:
    """The pattern that finds a name of `topic` in a text, by the rule of
    `topics.names_pattern`, or None when no name is long enough. With
    `people`, no bare name of a person is a name of the topic."""
    meta, _ = frontmatter.split(read_page(Path(wiki_root) / topic.path))
    aliases = topic_aliases(topic, meta, Path(wiki_root))
    return names_pattern(topic_names(topic, aliases, people=people))


# -- journal ----------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    rel: str
    day: date
    text: str


# One journal page: its path relative to the wiki root, its day, and its text.
JournalEntry = _Entry


def is_job_entry(meta: dict[str, Any]) -> bool:
    """True for the frontmatter of a journal entry that a job wrote."""
    return "run_id" in meta or meta.get("source") in JOB_SOURCES


def _journal_entries(data_root: Path, wiki_root: Path, today: date) -> list[_Entry]:
    """Every journal page up to `today`, except the pages a job run wrote and
    the pages that a digest covers."""
    journal = layout.journal_root(data_root)
    entries: list[_Entry] = []
    if not journal.is_dir():
        return entries
    for current, dirs, files in os.walk(journal):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.endswith(".md") or name.startswith("."):
                continue
            path = Path(current) / name
            day = layout.journal_day_from_path(path, data_root)
            if day is None or day > today:
                continue
            text = read_page(path)
            meta, _ = frontmatter.split(text)
            if is_job_entry(meta) or meta.get(COVERED_KEY):
                continue
            entries.append(_Entry(path.relative_to(wiki_root).as_posix(), day, text))
    return entries


def journal_entries(wiki_root: Path, today: date) -> list[JournalEntry]:
    """Every journal page dated up to `today`, except the pages a job run wrote
    and the pages that a digest covers."""
    wiki_root = Path(wiki_root)
    return _journal_entries(wiki_root.parent, wiki_root, today)


async def _topic_vectors(
    topic: Topic, text: str, settings: JoshuaConfig
) -> list[list[float]] | None:
    """The vectors of the chunks of the topic page, or None when the
    embedding model is not available."""
    try:
        chunks = await embed_document(
            topic.title,
            text,
            model=settings.memory.embed_model,
            chunk_chars=settings.memory.chunk_chars,
        )
    except RuntimeError as exc:
        logger.warning({"message": "bundle has no vector match", "error": str(exc)})
        return None
    return [vec for _, _, vec in chunks]


async def _near_journal_paths(
    vectors: list[list[float]] | None, repo: BundleStore, settings: JoshuaConfig
) -> dict[str, float]:
    """The journal pages with a chunk at or above `jobs.bundle.sim` to the topic
    page, each with its best similarity."""
    if not vectors:
        return {}
    near = await repo.kb_near_paths(vectors, kinds=[JOURNAL_KIND], min_sim=settings.jobs.bundle.sim)
    return {
        path[len(_INDEX_WIKI_PREFIX) :]: float(sim)
        for path, sim in near.items()
        if path.startswith(_INDEX_WIKI_PREFIX)
    }


def _about_topic(
    entries: list[_Entry],
    *,
    topic: Topic,
    aliases: Sequence[str],
    near: dict[str, float],
    settings: JoshuaConfig,
) -> dict[str, str]:
    """`{entry path: how it got in}` for the journal entries of the bundle."""
    cfg = settings.jobs.bundle
    by_title = names_pattern(topic_names(topic, people=settings.people))
    by_alias = names_pattern(aliases)
    via: dict[str, str] = {}
    vector_only: list[_Entry] = []
    for entry in entries:
        stem = PurePosixPath(entry.rel).stem
        if any(fnmatchcase(stem, glob) for glob in cfg.exclude_slugs):
            continue
        if by_title is not None and by_title.search(entry.text):
            via[entry.rel] = VIA_TITLE
        elif by_alias is not None and by_alias.search(entry.text):
            via[entry.rel] = VIA_ALIAS
        elif entry.rel in near:
            vector_only.append(entry)
    vector_only.sort(key=lambda e: (-near[e.rel], e.rel))
    for entry in vector_only[: cfg.max_vector_entries]:
        via[entry.rel] = f"{VIA_VECTOR} {near[entry.rel]:.2f}"
    return via


# -- snapshots --------------------------------------------------------------


def job_snapshots(settings: JoshuaConfig) -> dict[str, Snapshot]:
    """`{name: snapshot}` of the snapshots that a job may see: scope `shared`,
    or `jobs: true`."""
    return {snap.name: snap for snap in settings.memory.snapshots if snap.in_jobs}


def snapshot_part_path(name: str) -> str:
    """The bundle path of the part of one snapshot: `snapshots/<name>`."""
    return f"{SNAPSHOTS_FOLDER}/{name}"


def snapshot_line(chunk: KbChunk) -> str:
    """The item of a snapshot chunk on one line: its lines joined with `; `,
    at most `SNAPSHOT_LINE_CHARS` characters."""
    lines = [" ".join(line.split()) for line in item_body(chunk.text or "").splitlines()]
    return "; ".join(line for line in lines if line)[:SNAPSHOT_LINE_CHARS]


@dataclass(frozen=True)
class SnapshotItems:
    """The snapshot items near a query: the chunks, the closest first, and
    `{name: time of the latest good fetch}` of their snapshots."""

    chunks: list[KbChunk]
    times: dict[str, datetime]


async def near_snapshot_items(
    store: BundleStore,
    vectors: Sequence[list[float]],
    allowed: dict[str, Snapshot],
    *,
    min_sim: float,
    limit: int,
) -> SnapshotItems:
    """The snapshot items at `min_sim` or more to any of `vectors`, the
    closest first, at most `limit`, one chunk per item.

    Only the snapshots in `allowed` count, and a row must have the scope of
    its config entry. A snapshot with no good fetch is left out. A failed
    query raises; the caller decides what that means.
    """
    if not vectors or not allowed or limit <= 0:
        return SnapshotItems([], {})
    rows = await store.kb_near_snapshots(
        vectors,
        names=sorted(allowed),
        min_sim=min_sim,
        limit=limit * _SNAPSHOT_QUERY_FACTOR,
    )
    seen: set[str] = set()
    kept: list[KbChunk] = []
    for row in sorted(rows, key=lambda c: (-(c.similarity or 0.0), c.path, c.chunk_index)):
        snap = allowed.get(row.title)
        if snap is None or row.person_id != snap.person_id or row.path in seen:
            continue
        if (row.similarity or 0.0) < min_sim:
            continue
        seen.add(row.path)
        kept.append(row)
    names = sorted({c.title for c in kept})
    times = await store.snapshot_times(names) if names else {}
    kept = shown(kept, times)[:limit]
    return SnapshotItems(kept, {n: t for n, t in times.items() if n in names})


def snapshot_parts(
    items: SnapshotItems, allowed: dict[str, Snapshot], settings: JoshuaConfig
) -> list[SnapshotPart]:
    """One part per snapshot, in the order of its closest item. The block
    stops at the first item that does not fit in `jobs.bundle.snapshot_chars`."""
    budget = settings.jobs.bundle.snapshot_chars
    used = 0
    lines: dict[str, list[str]] = {}
    sims: dict[str, dict[str, float]] = {}
    heads: dict[str, str] = {}
    for chunk in items.chunks:
        name = chunk.title
        line = snapshot_line(chunk)
        if not line:
            continue
        cost = len(line) + 3
        head = ""
        if name not in heads:
            snap = allowed[name]
            head = (
                f"Live snapshot of {snap.server} {snap.tool} as of "
                f"{as_of(items.times[name], settings.timezone)}."
            )
            cost += len(head) + 2
        if used + cost > budget:
            break
        used += cost
        if head:
            heads[name] = head
        lines.setdefault(name, []).append(f"- {line}")
        sims.setdefault(name, {})[chunk.path] = round(float(chunk.similarity or 0.0), 4)
    return [
        SnapshotPart(
            name=name,
            path=snapshot_part_path(name),
            text=heads[name] + "\n\n" + "\n".join(lines[name]),
            items=sims[name],
        )
        for name in heads
    ]


def named_or_strong(
    items: SnapshotItems,
    patterns: Sequence[re.Pattern[str] | None],
    *,
    strong_sim: float,
    limit: int,
) -> SnapshotItems:
    """The items that name the topic (a title or an alias, by `patterns`) or
    that are at `strong_sim` or more, the closest first, at most `limit`.
    A short item of live state is close to many pages by meaning alone, so a
    similarity floor by itself lets in items about other things."""
    kept = [
        chunk
        for chunk in items.chunks
        if (chunk.similarity or 0.0) >= strong_sim
        or any(p is not None and p.search(item_body(chunk.text or "")) for p in patterns)
    ][:limit]
    names = {chunk.title for chunk in kept}
    return SnapshotItems(kept, {n: t for n, t in items.times.items() if n in names})


async def _snapshot_block(
    vectors: list[list[float]] | None,
    repo: BundleStore,
    settings: JoshuaConfig,
    patterns: Sequence[re.Pattern[str] | None] = (),
) -> tuple[list[SnapshotPart], list[str]]:
    """The snapshot parts of a bundle and the notes. It never fails."""
    cfg = settings.jobs.bundle
    allowed = job_snapshots(settings)
    if not allowed or cfg.snapshot_items <= 0 or cfg.snapshot_chars <= 0:
        return [], []
    if vectors is None:
        return [], [NO_SNAPSHOT_MODEL]
    try:
        near = await near_snapshot_items(
            repo,
            vectors,
            allowed,
            min_sim=cfg.snapshot_sim,
            limit=cfg.snapshot_items * _SNAPSHOT_QUERY_FACTOR,
        )
    except Exception as exc:  # noqa: BLE001 — a snapshot never fails a bundle
        logger.warning({"message": "bundle snapshot search failed", "error": str(exc)})
        return [], [NO_SNAPSHOT_SEARCH]
    items = named_or_strong(
        near, patterns, strong_sim=cfg.snapshot_strong_sim, limit=cfg.snapshot_items
    )
    return snapshot_parts(items, allowed, settings), []


def snapshot_summary(bundle: Bundle) -> str:
    """`Snapshots: <name> (<n> items), ...` for a report, or `Snapshots: none.`"""
    if not bundle.snapshots:
        return "Snapshots: none."
    shown_parts = []
    for part in bundle.snapshots:
        count = len(part.items)
        shown_parts.append(f"{part.name} ({count} item{'' if count == 1 else 's'})")
    return "Snapshots: " + ", ".join(shown_parts) + "."


# -- knowledge, questions, and insights ------------------------------------


def _knowledge_parts(topic: Topic, data_root: Path, wiki_root: Path) -> list[_Part]:
    dossier = layout.knowledge_dossier(topic.path, data_root)
    parts: list[_Part] = []
    if dossier.is_file():
        parts.append(_Part(dossier.relative_to(wiki_root).as_posix(), read_page(dossier)))
    folder = dossier.with_suffix("")
    if folder.is_dir():
        for page in sorted(folder.glob("*.md")):
            if page.is_file() and not page.name.startswith("."):
                parts.append(_Part(page.relative_to(wiki_root).as_posix(), read_page(page)))
    return parts


def _question_parts(topic: Topic, data_root: Path, wiki_root: Path) -> list[_Part]:
    root = layout.questions_root(data_root)
    parts: list[_Part] = []
    if not root.is_dir():
        return parts
    for page in sorted(root.rglob("*.md")):
        if not page.is_file() or layout.is_hidden(page, root):
            continue
        meta, _ = frontmatter.split(read_page(page))
        question, answer = meta.get("question"), meta.get("answer")
        if meta.get("topic") != topic.path or meta.get("status") != ANSWERED_QUESTION:
            continue
        if not isinstance(question, str) or not isinstance(answer, str) or not answer.strip():
            continue
        text = f"Question: {' '.join(question.split())}\n\nAnswer: {answer.strip()}"
        parts.append(_Part(page.relative_to(wiki_root).as_posix(), text))
    return parts


def _insight_parts(topic: Topic, data_root: Path, wiki_root: Path) -> list[_Part]:
    root = layout.insights_root(data_root)
    parts: list[_Part] = []
    if not root.is_dir():
        return parts
    for page in sorted(root.rglob("*.md")):
        if not page.is_file() or layout.is_hidden(page, root):
            continue
        text = read_page(page)
        meta, _ = frontmatter.split(text)
        if meta.get("topic") == topic.path and meta.get("status") in OPEN_INSIGHT_STATUSES:
            parts.append(_Part(page.relative_to(wiki_root).as_posix(), text))
    return parts


# -- the bundle -------------------------------------------------------------


def _local_day(now: datetime, settings: JoshuaConfig) -> date:
    if now.tzinfo is None:
        return now.date()
    return now.astimezone(ZoneInfo(settings.timezone)).date()


async def build_bundle(
    topic: Topic,
    *,
    wiki_root: Path,
    repo: BundleStore,
    settings: JoshuaConfig,
    now: datetime,
) -> Bundle:
    """Build the memory bundle of `topic`. See the module docstring for the rules.

    `wiki_root` is `layout.wiki_root(<data root>)`. `repo` answers the vector
    queries; the topic page is embedded once, with the indexer's own path.
    When the embedding model is not available, the vector match and the
    snapshot block are left out and the text match still runs. `now` sets the
    last journal day.
    """
    wiki_root = Path(wiki_root)
    data_root = wiki_root.parent
    if layout.wiki_root(data_root) != wiki_root:
        raise ValueError(f"not a wiki root: {wiki_root}")
    topic_text = read_page(wiki_root / topic.path)
    meta, _ = frontmatter.split(topic_text)
    aliases = drop_people(topic_aliases(topic, meta, wiki_root), settings.people)

    parts: list[_Part] = [_Part(topic.path, topic_text)]
    parts.extend(
        _Part(rel, read_page(wiki_root / rel))
        for rel in _linked_pages(topic, topic_text, wiki_root)
    )

    vectors = await _topic_vectors(topic, topic_text, settings)
    patterns = (names_pattern(topic_names(topic, people=settings.people)), names_pattern(aliases))
    snapshots, notes = await _snapshot_block(vectors, repo, settings, patterns)
    parts.extend(_Part(part.path, part.text) for part in snapshots)

    near = await _near_journal_paths(vectors, repo, settings)
    every = _journal_entries(data_root, wiki_root, _local_day(now, settings))
    via = _about_topic(every, topic=topic, aliases=aliases, near=near, settings=settings)
    entries = [entry for entry in every if entry.rel in via]
    entries.sort(key=lambda e: e.rel)
    entries.sort(key=lambda e: e.day, reverse=True)
    parts.extend(_Part(e.rel, e.text) for e in entries)

    parts.extend(_knowledge_parts(topic, data_root, wiki_root))
    parts.extend(_question_parts(topic, data_root, wiki_root))
    parts.extend(_insight_parts(topic, data_root, wiki_root))

    limit = settings.jobs.bundle.max_chars
    rendered = [parts[0].render()]
    paths = [parts[0].path]
    total = len(rendered[0])
    truncated = False
    for part in parts[1:]:
        piece = part.render()
        # Each later part costs its own text and the blank line before it.
        if total + 1 + len(piece) > limit:
            truncated = True
            break
        rendered.append(piece)
        paths.append(part.path)
        total += 1 + len(piece)
    text = "\n".join(rendered)
    return Bundle(
        topic=topic,
        paths=paths,
        text=text,
        chars=len(text),
        truncated=truncated,
        aliases=aliases,
        journal_via={p: via[p] for p in paths if p in via},
        snapshots=[part for part in snapshots if part.path in paths],
        notes=notes,
    )
