"""Which job pages a chat turn may get through retrieval.

The per-turn injection and the `search_memory` tool leave out the kinds
`insight` and `question` in the search itself. `ChatFilter` is the second
line: it drops a chunk of kind `insight` when its insight is not due on the
day of the turn (it is dismissed, expired, or snoozed), and a chunk of kind
`question` when its question is not `open`. A chunk whose page is gone or
does not parse is dropped too. Every other kind is kept.

The rule reads the frontmatter of the page from the data volume, through
`load_insight` and `load_question`, when the search runs. One `ChatFilter`
keeps what it read, so a page is read once in one search. The pages stay on
the disk and in the wiki, and `POST /v1/memory/search` does not use the rule.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from joshua_shared import layout

from joshua_core.jobs.insights.page import load_insight
from joshua_core.jobs.research.questions import load_question
from joshua_core.memory.models import KbChunk

INSIGHT_KIND = "insight"
QUESTION_KIND = "question"

_INSIGHT_PREFIX = f"wiki/{layout.INSIGHTS}/"
_QUESTION_PREFIX = f"wiki/{layout.QUESTIONS}/"


def _page_id(path: str, prefix: str) -> str | None:
    """`<section>/<slug>` from a chunk path under `prefix`, or None."""
    if not path.startswith(prefix) or not path.endswith(".md"):
        return None
    return path[len(prefix) : -len(".md")]


class ChatFilter:
    """The retrieval rule of one search, for one day. Call it with a chunk:
    True keeps the chunk."""

    def __init__(self, data_root: Path | str, today: date) -> None:
        self._data_root = Path(data_root)
        self._today = today
        self._seen: dict[tuple[str, str], bool] = {}

    def __call__(self, chunk: KbChunk) -> bool:
        if chunk.kind == INSIGHT_KIND:
            return self._check(INSIGHT_KIND, chunk.path)
        if chunk.kind == QUESTION_KIND:
            return self._check(QUESTION_KIND, chunk.path)
        return True

    def _check(self, kind: str, path: str) -> bool:
        key = (kind, path)
        if key not in self._seen:
            self._seen[key] = self._read(kind, path)
        return self._seen[key]

    def _read(self, kind: str, path: str) -> bool:
        if kind == INSIGHT_KIND:
            insight_id = _page_id(path, _INSIGHT_PREFIX)
            found = load_insight(insight_id, self._data_root) if insight_id else None
            return found is not None and found.insight.is_due(self._today)
        question_id = _page_id(path, _QUESTION_PREFIX)
        question = load_question(question_id, self._data_root) if question_id else None
        return question is not None and question.page.status == "open"
