"""Insights: where what we do disagrees with what is known, now.

`page` holds the insight page, the table of every insight, and expiry,
`knowledge` the published knowledge pages of a topic, `worker` the `insight`
worker, `prompts` its prompt, `gate` the rules an insight must pass, and `job`
the run itself and the pass that a research run also runs. See the insights
section of `docs/jobs.md`.
"""

from __future__ import annotations

from joshua_core.jobs.insights.gate import Checked, InsightGate, Verdict, cap, we_do_parts
from joshua_core.jobs.insights.job import (
    InsightPass,
    InsightsJob,
    PassResult,
    insight_model,
)
from joshua_core.jobs.insights.knowledge import KnownPage, normalize_page, published_pages
from joshua_core.jobs.insights.page import (
    OPEN_STATUSES,
    STATUSES,
    Insight,
    InsightFile,
    expire,
    insight_section,
    load_insight,
    load_insights,
    render_index,
    set_fields,
    write_index,
)
from joshua_core.jobs.insights.worker import InsightAnswer, RaisedInsight, raise_insights

__all__ = [
    "OPEN_STATUSES",
    "STATUSES",
    "Checked",
    "Insight",
    "InsightAnswer",
    "InsightFile",
    "InsightGate",
    "InsightPass",
    "InsightsJob",
    "KnownPage",
    "PassResult",
    "RaisedInsight",
    "Verdict",
    "cap",
    "expire",
    "insight_model",
    "insight_section",
    "load_insight",
    "load_insights",
    "normalize_page",
    "published_pages",
    "raise_insights",
    "render_index",
    "set_fields",
    "we_do_parts",
    "write_index",
]
