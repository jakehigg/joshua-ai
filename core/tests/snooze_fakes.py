"""A small wiki of insights and questions for the snooze, list, and retrieval
tests. Every page is invented: a backup check, a filter change, a smoke
alarm. `TODAY` is the local day of `jobs_fakes.NOW` in the instance timezone.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from joshua_core.jobs.insights.page import Body, Insight, insight_path, render_insight, write_text
from joshua_core.jobs.research.questions import QuestionPage, write_question

TODAY = date(2026, 10, 19)
TOPIC = "home/upkeep.md"
SECTION = "home"


def write_insight(data: Path, slug: str, **keys: Any) -> str:
    """Write one insight page and give its id."""
    values: dict[str, Any] = {
        "topic": TOPIC,
        "section": SECTION,
        "claim": f"The {slug.replace('-', ' ')} is not done.",
        "status": "new",
        "severity": "note",
        "confidence": "medium",
        "valid_from": TODAY - timedelta(days=30),
        "valid_until": TODAY + timedelta(days=60),
        "found_at": TODAY - timedelta(days=10),
        "seen_at": TODAY,
    }
    values.update(keys)
    insight = Insight.model_validate(values)
    insight_id = f"{SECTION}/{slug}"
    body = Body(why="It matters.", action="Do it.", evidence="", phrase_paths={}, known={})
    text = render_insight(insight_id, insight, body)
    write_text(insight_path(insight_id, data), text, data)
    return insight_id


def write_open_question(data: Path, slug: str, status: str = "open") -> str:
    """Write one question page and give its id."""
    page = QuestionPage.model_validate(
        {
            "question": f"When was the {slug.replace('-', ' ')} last done?",
            "topic": TOPIC,
            "section": SECTION,
            "status": status,
            "raised_at": TODAY - timedelta(days=3),
        }
    )
    question_id = f"{SECTION}/{slug}"
    write_question(question_id, page, data)
    return question_id
