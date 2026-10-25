"""The open items of a topic, for the topic hint of a chat turn.

The jobs keep insights and questions out of the per-turn injection. In place
of their text, a turn that a person sent gets one short line when it is about
a topic that has due insights or open questions:

    [Open items about Snake plant (houseplants/snake-plant.md): 1 insight,
    2 questions. Read them with list_insights(topic=...) / list_questions(topic=...)
    before you mention one.]

The line holds only the counts, never a claim or a question.

A turn is about a topic when a chunk of the search is the topic page itself,
or when the text of the turn holds a name of the topic, by the name rule of
the research `mentioned` selection (`bundle.topic_pattern`). The rank of the
search goes first, then the names in topic order. A turn gets
`MAX_TOPICS` topics at most.

`OpenItems` reads the frontmatter of the insight and question pages with
`load_insights` and `load_questions`, and keeps the map of topic to counts for
`ttl` seconds, so a turn does not read every page.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from joshua_shared import layout
from joshua_shared.config import Person

from joshua_core.jobs.bundle import topic_pattern
from joshua_core.jobs.insights.page import load_insights
from joshua_core.jobs.research.questions import load_questions
from joshua_core.jobs.topics import load_topic

# A turn names at most this many topics in its hint line.
MAX_TOPICS = 2
# How long the map of topic to counts is kept, in seconds.
CACHE_SECONDS = 60.0
INSIGHTS_TOOL = "list_insights"
_WIKI_PREFIX = "wiki/"
QUESTIONS_TOOL = "list_questions"


@dataclass(frozen=True)
class TopicItems:
    """The open items of one topic: its path relative to the wiki root, its
    title, the due insights, and the open questions."""

    topic: str
    title: str
    insights: int
    questions: int
    pattern: re.Pattern[str] | None = None

    def audit(self) -> dict[str, object]:
        """The record of this topic for the `kb_event` row."""
        return {"topic": self.topic, "insights": self.insights, "questions": self.questions}


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def hint_line(items: Sequence[TopicItems]) -> str:
    """The one line of the hint for `items`, or `""` when there is none."""
    if not items:
        return ""
    parts = []
    tools = []
    for item in items:
        counts = []
        if item.insights:
            counts.append(_plural(item.insights, "insight"))
        if item.questions:
            counts.append(_plural(item.questions, "question"))
        parts.append(f"{item.title} ({item.topic}): {', '.join(counts)}")
    if any(item.insights for item in items):
        tools.append(f"{INSIGHTS_TOOL}(topic=...)")
    if any(item.questions for item in items):
        tools.append(f"{QUESTIONS_TOOL}(topic=...)")
    return (
        f"[Open items about {'; '.join(parts)}. Read them with "
        f"{' / '.join(tools)} before you mention one.]"
    )


class OpenItems:
    """The topics that have due insights or open questions, kept for `ttl`
    seconds.

    `insights` and `questions` say which kinds to count: the kind of a job
    that is off is not counted. `today` gives the day in the instance
    timezone, for the snooze of an insight.
    """

    def __init__(
        self,
        data_root: Path | str,
        *,
        today: Callable[[], date],
        people: Sequence[Person] | None = None,
        insights: bool = True,
        questions: bool = True,
        ttl: float = CACHE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._data_root = Path(data_root)
        self._today = today
        self._people = list(people or [])
        self._insights = insights
        self._questions = questions
        self._ttl = ttl
        self._monotonic = monotonic
        self._cached: dict[str, TopicItems] | None = None
        self._read_at = 0.0

    def topics(self) -> dict[str, TopicItems]:
        """`{topic path: TopicItems}` for each topic with an open item."""
        now = self._monotonic()
        if self._cached is None or now - self._read_at >= self._ttl:
            self._cached = self._read()
            self._read_at = now
        return self._cached

    def _read(self) -> dict[str, TopicItems]:
        today = self._today()
        insights: dict[str, int] = {}
        questions: dict[str, int] = {}
        if self._insights:
            for item in load_insights(self._data_root):
                if item.insight.is_due(today):
                    insights[item.insight.topic] = insights.get(item.insight.topic, 0) + 1
        if self._questions:
            for page in load_questions(self._data_root):
                if page.page.status == "open":
                    questions[page.page.topic] = questions.get(page.page.topic, 0) + 1
        wiki = layout.wiki_root(self._data_root)
        out: dict[str, TopicItems] = {}
        for path in sorted({*insights, *questions}):
            topic = load_topic(path, wiki)
            out[path] = TopicItems(
                topic=path,
                title=topic.title,
                insights=insights.get(path, 0),
                questions=questions.get(path, 0),
                pattern=topic_pattern(topic, wiki, people=self._people),
            )
        return out

    def about(self, text: str, ranked_paths: Iterable[str]) -> list[TopicItems]:
        """The topics with open items that a turn is about, `MAX_TOPICS` at
        most. `ranked_paths` are the data-relative paths of the search chunks,
        in rank order."""
        topics = self.topics()
        if not topics:
            return []
        found: dict[str, TopicItems] = {}
        for path in ranked_paths:
            rel = path[len(_WIKI_PREFIX) :] if path.startswith(_WIKI_PREFIX) else ""
            item = topics.get(rel)
            if item is not None:
                found.setdefault(rel, item)
        for rel, item in topics.items():
            if rel not in found and item.pattern is not None and item.pattern.search(text):
                found[rel] = item
        return list(found.values())[:MAX_TOPICS]
