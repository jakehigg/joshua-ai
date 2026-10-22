"""The candidates of the digest job and their groups.

A candidate is a journal entry dated before today minus
`jobs.digest.min_age_days`. These entries are never candidates:

- a nightly day page, `YYYY/MM/DD/YYYY-MM-DD.md`;
- the report of a job run (`bundle.is_job_entry`);
- an entry that a digest already covers: its frontmatter has `digest`;
- a digest entry: its `source` is `digest`;
- an entry whose frontmatter block the job cannot add a key to.

The groups come from two rules, in this order:

1. The slug stem. The stem of an entry is its file slug without the tokens
   that name a date: numbers, ordinals such as `3rd`, and the names of the
   weekdays and the months. So `filter-check-monday-march-3` and
   `filter-check-tuesday-march-4` have the stem `filter-check`. The entries
   with one stem are one group when there are at least `min_entries` of them.
2. The meaning. Each other entry is embedded once (the mean of its chunk
   vectors, from `memory.indexer.embed_document`). In date order, an entry
   joins the group whose first entry is closest to it, when the cosine is at
   or above `sim`; else it starts a new group.

A group with more than `max_entries` entries is split into runs in date
order, of sizes that differ by one at most. A group or a run with fewer than
`min_entries` entries is dropped. The groups are then ordered by size, the
largest first, then by the first day and the first path, and the run takes
the first `max_groups`. The same wiki on the same day gives the same groups.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from joshua_shared import frontmatter, layout

from joshua_core.jobs.bundle import is_job_entry

# The `source` of a digest entry, and the frontmatter keys of the job.
DIGEST_SOURCE = "digest"
COVERED_KEY = "digest"
BY_SLUG = "slug"
BY_MEANING = "meaning"

# The reasons an entry is not a candidate.
SKIP_DAY_PAGE = "a nightly day page"
SKIP_NEW = "newer than jobs.digest.min_age_days"
SKIP_REPORT = "a job report"
SKIP_COVERED = "already in a digest"
SKIP_DIGEST = "a digest entry"
SKIP_FRONTMATTER = "the frontmatter block cannot take a key"
SKIP_NO_VECTOR = "no vector"

_MONTHS = (
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec"
).split()
_WEEKDAYS = (
    "monday tuesday wednesday thursday friday saturday sunday "
    "mon tue tues wed thu thur thurs fri sat sun"
).split()
_DATE_WORDS = frozenset(_MONTHS + _WEEKDAYS)
_DATE_TOKEN_RE = re.compile(r"^\d+(?:st|nd|rd|th)?\Z")
_H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class Entry:
    """One journal entry: its path relative to the wiki root, its day, its
    whole text, its frontmatter, and its body."""

    rel: str
    day: date
    text: str
    meta: dict[str, Any]
    body: str

    @property
    def slug(self) -> str:
        return PurePosixPath(self.rel).stem

    @property
    def people(self) -> list[str]:
        raw = self.meta.get("people")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [str(p).strip() for p in raw if str(p).strip()]

    @property
    def title(self) -> str:
        """The first `# ` heading of the body, or the slug: the title the
        indexer gives the page."""
        for line in self.body.splitlines():
            match = _H1_RE.match(line)
            if match:
                return match.group(1).strip()
        return self.slug


def read_exact(path: Path) -> str:
    """The text of a page with its line ends as they are on the disk, or `""`
    when it cannot be read as UTF-8."""
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def write_exact(path: Path, text: str) -> None:
    """Write `text` with its line ends as they are."""
    path.write_bytes(text.encode("utf-8"))


def sort_key(entry: Entry) -> tuple[date, str]:
    return (entry.day, entry.rel)


@dataclass(frozen=True)
class Group:
    """Entries that one digest covers, in date order. `key` is the slug stem
    for a group `by` slug, and the slug of the first entry for a group `by`
    meaning."""

    key: str
    by: str
    entries: tuple[Entry, ...]

    @property
    def first(self) -> date:
        return self.entries[0].day

    @property
    def last(self) -> date:
        return self.entries[-1].day

    @property
    def size(self) -> int:
        return len(self.entries)

    @property
    def paths(self) -> list[str]:
        return [e.rel for e in self.entries]


@dataclass
class Candidates:
    """The candidates, and the paths that are not, by reason."""

    entries: list[Entry] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)

    def skip(self, reason: str, rel: str) -> None:
        self.skipped.setdefault(reason, []).append(rel)


def is_digest_entry(meta: dict[str, Any]) -> bool:
    """True for the frontmatter of a digest entry. The report of a digest run
    has `source: digest` too, and its `run_id` tells it apart."""
    return meta.get("source") == DIGEST_SOURCE and "run_id" not in meta


def is_covered(meta: dict[str, Any]) -> bool:
    """True for the frontmatter of an entry that a digest covers."""
    return bool(meta.get(COVERED_KEY))


def _can_mark(text: str) -> bool:
    try:
        frontmatter.update(text, **{COVERED_KEY: "x"})
    except ValueError:
        return False
    return True


def find_candidates(data_root: Path, today: date, min_age_days: int) -> Candidates:
    """The candidates under `wiki/journal/`, in date order. See the module
    docstring for the rules."""
    data_root = Path(data_root)
    wiki = layout.wiki_root(data_root)
    journal = layout.journal_root(data_root)
    cutoff = today - timedelta(days=min_age_days)
    out = Candidates()
    if not journal.is_dir():
        return out
    for current, dirs, files in os.walk(journal):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if not name.endswith(".md") or name.startswith("."):
                continue
            path = Path(current) / name
            day = layout.journal_day_from_path(path, data_root)
            if day is None:
                continue
            rel = path.relative_to(wiki).as_posix()
            if path == layout.journal_day_page(day, data_root):
                out.skip(SKIP_DAY_PAGE, rel)
                continue
            if day >= cutoff:
                out.skip(SKIP_NEW, rel)
                continue
            text = read_exact(path)
            meta, body = frontmatter.split(text)
            if is_job_entry(meta):
                out.skip(SKIP_REPORT, rel)
            elif is_covered(meta):
                out.skip(SKIP_COVERED, rel)
            elif is_digest_entry(meta):
                out.skip(SKIP_DIGEST, rel)
            elif not _can_mark(text):
                out.skip(SKIP_FRONTMATTER, rel)
            else:
                out.entries.append(Entry(rel, day, text, meta, body))
    out.entries.sort(key=sort_key)
    return out


def slug_stem(slug: str) -> str:
    """`slug` without the tokens that name a date. See the module docstring."""
    kept = [
        token
        for token in slug.lower().split("-")
        if token and token not in _DATE_WORDS and not _DATE_TOKEN_RE.match(token)
    ]
    return "-".join(kept)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def mean_vector(vectors: Sequence[Sequence[float]]) -> list[float] | None:
    if not vectors:
        return None
    width = len(vectors[0])
    return [sum(v[i] for v in vectors) / len(vectors) for i in range(width)]


# Gives the vector of one entry, or None. It raises `RuntimeError` when the
# embedding model is not available.
Embedder = Callable[[Entry], Awaitable[list[float] | None]]


def split_runs(entries: Sequence[Entry], max_entries: int) -> list[list[Entry]]:
    """`entries` in date order, in runs of at most `max_entries`, of sizes
    that differ by one at most."""
    ordered = sorted(entries, key=sort_key)
    count = math.ceil(len(ordered) / max_entries) if ordered else 0
    runs: list[list[Entry]] = []
    start = 0
    for i in range(count):
        size = len(ordered) // count + (1 if i < len(ordered) % count else 0)
        runs.append(ordered[start : start + size])
        start += size
    return runs


@dataclass
class Grouping:
    """The groups that a run takes, the groups over `max_groups`, the
    candidates in no group, the entries with no vector, and the notes."""

    groups: list[Group] = field(default_factory=list)
    over: list[Group] = field(default_factory=list)
    alone: list[str] = field(default_factory=list)
    no_vector: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


async def build_groups(
    entries: Sequence[Entry],
    *,
    embed: Embedder,
    sim: float,
    min_entries: int,
    max_entries: int,
    max_groups: int,
) -> Grouping:
    """The groups of `entries`. See the module docstring for the rules."""
    ordered = sorted(entries, key=sort_key)
    result = Grouping()
    by_stem: dict[str, list[Entry]] = {}
    rest: list[Entry] = []
    for entry in ordered:
        stem = slug_stem(entry.slug)
        if stem:
            by_stem.setdefault(stem, []).append(entry)
        else:
            rest.append(entry)
    raw: list[Group] = []
    for stem, members in by_stem.items():
        if len(members) >= min_entries:
            raw.append(Group(stem, BY_SLUG, tuple(members)))
        else:
            rest.extend(members)
    rest.sort(key=sort_key)

    clusters: list[tuple[list[float], list[Entry]]] = []
    try:
        for entry in rest:
            vector = await embed(entry)
            if vector is None:
                result.no_vector.append(entry.rel)
                continue
            best: int | None = None
            best_sim = sim
            for i, (seed, _) in enumerate(clusters):
                value = cosine(vector, seed)
                if value >= best_sim and (best is None or value > best_sim):
                    best, best_sim = i, value
            if best is None:
                clusters.append((vector, [entry]))
            else:
                clusters[best][1].append(entry)
    except RuntimeError as exc:
        result.notes.append(f"The entries were not grouped by meaning: {exc}.")
        clusters = []
    for _, members in clusters:
        if len(members) >= min_entries:
            raw.append(Group(members[0].slug, BY_MEANING, tuple(members)))

    kept: list[Group] = []
    for group in raw:
        runs = split_runs(group.entries, max_entries)
        if len(runs) == 1:
            kept.append(group)
            continue
        for run in runs:
            if len(run) >= min_entries:
                key = group.key if group.by == BY_SLUG else run[0].slug
                kept.append(Group(key, group.by, tuple(run)))
    kept.sort(key=lambda g: (-g.size, g.first, g.entries[0].rel))
    result.groups = kept[:max_groups]
    result.over = kept[max_groups:]
    taken = {rel for g in kept for rel in g.paths}
    no_vector = set(result.no_vector)
    result.alone = [e.rel for e in ordered if e.rel not in taken and e.rel not in no_vector]
    return result


__all__ = [
    "BY_MEANING",
    "BY_SLUG",
    "COVERED_KEY",
    "DIGEST_SOURCE",
    "Candidates",
    "Embedder",
    "Entry",
    "Group",
    "Grouping",
    "build_groups",
    "cosine",
    "find_candidates",
    "is_covered",
    "is_digest_entry",
    "mean_vector",
    "read_exact",
    "slug_stem",
    "split_runs",
    "write_exact",
]
