"""Offline fakes and a fixture wiki for the insights tests.

`build_insights_wiki` writes a wiki with an irrigation topic that runs the
summer program, a journal entry about it, a published knowledge page about
autumn watering, and a roses topic with no knowledge. `FakeInsightWorker`
answers the `insight` worker, records its calls, and adds a fixed usage.
`claim_vector` is a tiny embedding: claims with the same key words are close.
"""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any

from jobs_fakes import make_settings
from joshua_core.engine.usage import Usage
from joshua_core.jobs.insights.prompts import INSIGHT_PROMPT

IRRIGATION = "garden/irrigation.md"
ROSES = "garden/roses.md"
LOG = "journal/2026/10/15/garden-weather-log-thursday-october-15.md"
AUTUMN = "garden/irrigation/autumn-watering.md"
DRAFT = "garden/irrigation/draft-page.md"
LAWN_PAGE = "garden/lawn/mowing.md"
TODAY = date(2026, 10, 19)

INSIGHTS_ON = "jobs:\n  enabled: true\n  insights:\n    enabled: true\n"

PHRASE = "every zone on the 20-minute summer program"
LOG_PHRASE = "still runs the summer program"
CLAIM = "The garden is still on the 20-minute summer program in late October."

INSIGHT_USAGE = Usage(input=300, output=50)

AUTUMN_TEXT = (
    "---\n"
    "question: How long should the zones run in autumn?\n"
    "answer_short: Cut each zone to about half the summer time from October.\n"
    "topic: garden/irrigation.md\n"
    "researched_at: 2026-10-01\n"
    "refresh_after: 2027-03-30\n"
    "confidence: high\n"
    "status: published\n"
    "sources:\n"
    "  - url: https://extension.university.example/autumn\n"
    "    title: Autumn watering\n"
    "    trust: primary\n"
    "source: research\n"
    "---\n"
    "# How long should the zones run in autumn?\n\n"
    "## Answer\n\nCut each zone to about half the summer time from October.\n"
)

DOSSIER_TEXT = (
    "---\n"
    "topic: garden/irrigation.md\n"
    "title: Irrigation\n"
    "questions:\n"
    "  - slug: autumn-watering\n"
    "    question: How long should the zones run in autumn?\n"
    "    status: answered\n"
    "    page: garden/irrigation/autumn-watering.md\n"
    "source: research\n"
    "---\n"
    "# Irrigation: what we researched\n"
)

WIKI: dict[str, str] = {
    "Home.md": "# Home\n\n- [Garden](garden.md)\n",
    "garden.md": "# Garden\n\n- [Irrigation](garden/irrigation.md)\n- [Roses](garden/roses.md)\n",
    IRRIGATION: (
        "# Irrigation\n\n"
        "The controller runs every zone on the 20-minute summer program, at 6 am.\n"
        "The drip line feeds the roses.\n"
    ),
    ROSES: "# Roses\n\nPruned in March. The bed is by the fence.\n",
    "garden/lawn.md": "# Lawn\n\nMowed on Saturdays.\n",
    LOG: (
        "---\ndate: 2026-10-15\n---\n"
        "Cool and dry. The irrigation still runs the summer program every morning.\n"
    ),
    "journal/2026/10/10/bread.md": "---\ndate: 2026-10-10\n---\nBaked bread.\n",
    "knowledge/garden/irrigation.md": DOSSIER_TEXT,
    f"knowledge/{AUTUMN}": AUTUMN_TEXT,
    f"knowledge/{DRAFT}": "---\nstatus: held\ntopic: garden/irrigation.md\n---\n# Draft\n",
    f"knowledge/{LAWN_PAGE}": AUTUMN_TEXT.replace("garden/irrigation.md", "garden/lawn.md"),
}


def build_insights_wiki(data_root: Path, pages: dict[str, str] | None = None) -> Path:
    wiki = data_root / "wiki"
    for rel, text in (pages if pages is not None else WIKI).items():
        path = wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return wiki


def insights_settings(extra: str = "") -> Any:
    return make_settings(INSIGHTS_ON + extra)


def item(**overrides: Any) -> dict[str, Any]:
    """One raised insight that passes the gate."""
    value: dict[str, Any] = {
        "claim": CLAIM,
        "why": "The beds stay wet in cool weather, and the roots rot.",
        "action": "Set each zone to 10 minutes on the controller.",
        "severity": "act",
        "valid_from": "2026-10-01",
        "valid_until": "2026-11-30",
        "we_do": [PHRASE],
        "we_do_paths": [IRRIGATION],
        "knowledge_says": [AUTUMN],
        "confidence": "high",
        "evidence": (
            f"[Our page]({IRRIGATION}) says the summer program. [The answer](knowledge/{AUTUMN}) "
            "says half. [Elsewhere](https://evil.example.com/x) and [[secret]].\n"
            "## A heading the worker planted"
        ),
    }
    value.update(overrides)
    return value


def answer(*items: dict[str, Any]) -> dict[str, Any]:
    return {"insights": list(items)}


class FakeInsightWorker:
    """The `insight` worker. `value` is the answer, a callable of the user
    text, or an exception to raise."""

    def __init__(self, value: Any = None) -> None:
        self.value = answer(item()) if value is None else value
        self.calls: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any] | None:
        if kwargs["system_prompt"] != INSIGHT_PROMPT:
            raise AssertionError("unknown worker")
        self.calls.append(kwargs["user_text"])
        self.kwargs.append(kwargs)
        out = kwargs.get("usage_out")
        if out is not None:
            out.append(INSIGHT_USAGE)
        value = self.value
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value(kwargs["user_text"])
        return copy.deepcopy(value)


def claim_vector(text: str) -> list[float]:
    """A tiny embedding of a few key words."""
    words = ("summer", "program", "october", "roses", "mulch", "frost", "lawn", "drip")
    low = text.lower()
    return [*(1.0 if w in low else 0.0 for w in words), 0.1]
