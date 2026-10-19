"""Which topic a research run takes. The choice is deterministic.

`select_topic` applies four rules in order and stops at the first that gives
a topic:

1. `person`: a topic whose dossier has an open question that a person asked.
2. `journal`: among the topics that a journal entry of the last
   `jobs.bundle.recent_days` days is about, the lowest coverage. A topic with
   no dossier has a coverage of 0.0. Ties go to the topic with the most
   entries.
3. `coverage`: among all topics, the lowest coverage below
   `jobs.research.coverage_target`.
4. `refresh`: the dossier with the oldest `refresh_after` that is before the
   day of the run.

Each remaining tie goes to the seeded pick: a shuffle of the sorted topic
paths with `random.Random(f"{run_date}:{sha256 of the sorted paths}")`. So the
same wiki on the same day gives the same topic, and another day can give
another topic among equals.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from joshua_core.jobs.bundle import journal_entries, topic_pattern
from joshua_core.jobs.research.dossier import Dossier
from joshua_core.jobs.topics import Topic

RULES = ("person", "journal", "coverage", "refresh")


@dataclass(frozen=True)
class Selection:
    """The topic a run takes, the rule that chose it, and the next best topic.

    `topic` is None when no rule gives a topic. `reason` is one sentence for
    the report.
    """

    topic: str | None
    rule: str | None
    runner_up: str | None
    reason: str


def seed_ranks(paths: Sequence[str], run_date: date) -> dict[str, int]:
    """The seeded rank of each topic path: a lower rank wins a tie."""
    ordered = sorted(set(paths))
    digest = hashlib.sha256("\n".join(ordered).encode("utf-8")).hexdigest()
    rng = random.Random(f"{run_date.isoformat()}:{digest}")
    shuffled = list(ordered)
    rng.shuffle(shuffled)
    return {path: rank for rank, path in enumerate(shuffled)}


def coverage_of(path: str, dossiers: Mapping[str, Dossier]) -> float:
    dossier = dossiers.get(path)
    return dossier.compute_coverage() if dossier is not None else 0.0


def journal_counts(
    topics: Sequence[Topic], wiki_root: Path, today: date, days: int
) -> dict[str, int]:
    """`{topic path: count}` of the journal entries of the last `days` days,
    up to `today`, that hold the title or an alias of the topic. The bundle
    rule for title and alias matches. The journal is read once."""
    since = today - timedelta(days=days)
    entries = [e for e in journal_entries(wiki_root, today) if e.day > since]
    counts: dict[str, int] = {}
    if not entries:
        return counts
    for topic in topics:
        pattern = topic_pattern(topic, wiki_root)
        if pattern is None:
            continue
        found = sum(1 for e in entries if pattern.search(e.text))
        if found:
            counts[topic.path] = found
    return counts


def _ordered(candidates: Sequence[str], key: Callable[[str], Any]) -> list[str]:
    return sorted(candidates, key=key)


def select_topic(
    topics: Sequence[Topic],
    dossiers: Mapping[str, Dossier],
    journal_recent: Mapping[str, int],
    run_date: date,
    settings: Any,
) -> Selection:
    """Choose the topic of a run. `settings` is the whole config.

    `topics` are the topic pages of the wiki. `dossiers` maps a topic path to
    its dossier. `journal_recent` is `journal_counts` for the run.
    """
    paths = [t.path for t in topics]
    if not paths:
        return Selection(None, None, None, "The wiki has no topic page.")
    rank = seed_ranks(paths, run_date)
    cfg = settings.jobs.research
    cov = {p: coverage_of(p, dossiers) for p in paths}

    def person() -> list[str]:
        found = [p for p in paths if p in dossiers and dossiers[p].has_person_question()]
        return _ordered(found, lambda p: rank[p])

    def journal() -> list[str]:
        found = [p for p in paths if journal_recent.get(p, 0) > 0]
        return _ordered(found, lambda p: (cov[p], -journal_recent[p], rank[p]))

    def coverage() -> list[str]:
        found = [p for p in paths if cov[p] < cfg.coverage_target]
        return _ordered(found, lambda p: (cov[p], rank[p]))

    def refresh() -> list[str]:
        found = [
            p
            for p in paths
            if p in dossiers
            and dossiers[p].refresh_after is not None
            and dossiers[p].refresh_after < run_date
        ]
        return _ordered(found, lambda p: (dossiers[p].refresh_after, rank[p]))

    days = settings.jobs.bundle.recent_days
    reasons: dict[str, Callable[[str], str]] = {
        "person": lambda p: f"A person asked a question about {p} that is still open.",
        "journal": lambda p: (
            f"{journal_recent.get(p, 0)} journal entries of the last {days} days are about "
            f"{p}, and its coverage is {cov[p]:.2f}, the lowest of those topics."
        ),
        "coverage": lambda p: (
            f"{p} has the lowest coverage, {cov[p]:.2f}, below the target "
            f"{cfg.coverage_target:.2f}."
        ),
        "refresh": lambda p: (
            f"The dossier of {p} was due for a refresh on {dossiers[p].refresh_after}."
        ),
    }
    lists = {"person": person(), "journal": journal(), "coverage": coverage(), "refresh": refresh()}
    for index, rule in enumerate(RULES):
        found = lists[rule]
        if not found:
            continue
        topic = found[0]
        runner_up = found[1] if len(found) > 1 else None
        if runner_up is None:
            for later in RULES[index + 1 :]:
                others = [p for p in lists[later] if p != topic]
                if others:
                    runner_up = others[0]
                    break
        return Selection(topic, rule, runner_up, reasons[rule](topic))
    return Selection(
        None,
        None,
        None,
        "No topic is due: no open question from a person, no topic in the recent "
        "journal, every coverage is at the target, and no dossier is due for a refresh.",
    )
