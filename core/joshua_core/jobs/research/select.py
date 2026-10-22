"""Which topic a research run takes. The choice is deterministic.

`select_topic` applies four rules in order and stops at the first that gives
a topic:

1. `person`: a topic whose dossier has an open question that a person asked.
2. `mentioned`: among the topics that the messages of people of the last
   `jobs.bundle.recent_days` days name, the lowest coverage. A topic with no
   dossier has a coverage of 0.0. Ties go to the topic with the most
   messages.
3. `coverage`: among all topics, the lowest coverage below
   `jobs.research.coverage_target`.
4. `refresh`: the dossier with the oldest `refresh_after` that is before the
   day of the run.

A person section is a first path segment in `jobs.research.person_sections`:
its pages are about people. Each rule takes a topic in a person section only
when no topic outside one qualifies under the same rule, and the reason then
says so.

Only what people say counts. A message is an inbound transcript row
(`direction` is `in`) from a person of the roster, a member or a guest. A
reply of Joshua, a scheduled task, an injected event, and the journal never
count: Joshua's own words are not a sign that a person cares about a topic.
A message is about a topic when it holds a name of the topic
(`bundle.topic_pattern`). A person's name is never a name of a topic, by the
roster rule of `topics`, so a message that only names a person counts for no
topic. A possessive title counts by its generic name.

Each remaining tie goes to the seeded pick: a shuffle of the sorted topic
paths with `random.Random(f"{run_date}:{sha256 of the sorted paths}")`. So the
same wiki on the same day gives the same topic, and another day can give
another topic among equals.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from joshua_shared.config import Person

from joshua_core.jobs.bundle import topic_pattern
from joshua_core.jobs.research.dossier import Dossier
from joshua_core.jobs.topics import Topic

RULES = ("person", "mentioned", "coverage", "refresh")

# The direction of a transcript row that a person sent.
INBOUND = "in"


class MessageStore(Protocol):
    """The one transcript query of the `mentioned` rule. `Repo` implements it."""

    async def inbound_messages(self, since: datetime) -> list[dict[str, Any]]: ...


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


def in_person_section(topic: Topic, person_sections: Sequence[str]) -> bool:
    """True when the first path segment of `topic` is a person section."""
    return bool(topic.section) and topic.section in person_sections


def people_messages(rows: Iterable[Mapping[str, Any]], people: Sequence[Person]) -> list[str]:
    """The text of each row that a person of `people` sent.

    A row counts only when its `direction` is `in` and it comes from a person
    of the roster: its `person_id` is the id of a person, or its `speaker` is
    the name of a person. Every other row, and every row of a sender who is
    not on the roster, is left out.
    """
    ids = {p.id for p in people}
    names = {p.name for p in people}
    out: list[str] = []
    for row in rows:
        if row.get("direction") != INBOUND:
            continue
        if row.get("person_id") not in ids and row.get("speaker") not in names:
            continue
        text = (row.get("content") or "").strip()
        if text:
            out.append(text)
    return out


def mention_counts(
    topics: Sequence[Topic],
    wiki_root: Path,
    messages: Sequence[str],
    *,
    people: Sequence[Person] | None = None,
) -> dict[str, int]:
    """`{topic path: count}` of the `messages` that hold a name of the topic.
    The bundle rule for title and alias matches. With `people`, the name of a
    person is not a name of a topic. A topic that no message names is left
    out."""
    counts: dict[str, int] = {}
    if not messages:
        return counts
    for topic in topics:
        pattern = topic_pattern(topic, wiki_root, people=people)
        if pattern is None:
            continue
        found = sum(1 for text in messages if pattern.search(text))
        if found:
            counts[topic.path] = found
    return counts


# The end of a reason for a topic in a person section.
PERSON_SECTION_TAIL = (
    " It is in a person section, and no topic outside a person section qualifies under this rule."
)


def _ordered(candidates: Sequence[str], key: Callable[[str], Any]) -> list[str]:
    return sorted(candidates, key=key)


def select_topic(
    topics: Sequence[Topic],
    dossiers: Mapping[str, Dossier],
    mentions: Mapping[str, int],
    run_date: date,
    settings: Any,
) -> Selection:
    """Choose the topic of a run. `settings` is the whole config.

    `topics` are the topic pages of the wiki. `dossiers` maps a topic path to
    its dossier. `mentions` is `mention_counts` for the run.
    """
    paths = [t.path for t in topics]
    if not paths:
        return Selection(None, None, None, "The wiki has no topic page.")
    rank = seed_ranks(paths, run_date)
    cfg = settings.jobs.research
    cov = {p: coverage_of(p, dossiers) for p in paths}
    about_people = {t.path for t in topics if in_person_section(t, cfg.person_sections)}

    def person() -> list[str]:
        found = [p for p in paths if p in dossiers and dossiers[p].has_person_question()]
        return _ordered(found, lambda p: (p in about_people, rank[p]))

    def mentioned() -> list[str]:
        found = [p for p in paths if mentions.get(p, 0) > 0]
        return _ordered(found, lambda p: (p in about_people, cov[p], -mentions[p], rank[p]))

    def coverage() -> list[str]:
        found = [p for p in paths if cov[p] < cfg.coverage_target]
        return _ordered(found, lambda p: (p in about_people, cov[p], rank[p]))

    def refresh() -> list[str]:
        found = [
            p
            for p in paths
            if p in dossiers
            and dossiers[p].refresh_after is not None
            and dossiers[p].refresh_after < run_date
        ]
        return _ordered(found, lambda p: (p in about_people, dossiers[p].refresh_after, rank[p]))

    days = settings.jobs.bundle.recent_days
    reasons: dict[str, Callable[[str], str]] = {
        "person": lambda p: f"A person asked a question about {p} that is still open.",
        "mentioned": lambda p: (
            f"{mentions.get(p, 0)} messages from people in the last {days} days name "
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
    lists = {
        "person": person(),
        "mentioned": mentioned(),
        "coverage": coverage(),
        "refresh": refresh(),
    }
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
        reason = reasons[rule](topic)
        if topic in about_people:
            reason += PERSON_SECTION_TAIL
        return Selection(topic, rule, runner_up, reason)
    return Selection(
        None,
        None,
        None,
        "No topic is due: no open question from a person, no topic in the recent "
        "messages from people, every coverage is at the target, and no dossier is due "
        "for a refresh.",
    )
