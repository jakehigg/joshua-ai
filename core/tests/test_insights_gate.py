"""The insight gate: each rule it applies, with the reason of each rejection,
the refresh of an open insight, a dismissed claim, and the cap."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from insights_fakes import (
    AUTUMN,
    CLAIM,
    DRAFT,
    IRRIGATION,
    LAWN_PAGE,
    LOG,
    LOG_PHRASE,
    PHRASE,
    TODAY,
    build_insights_wiki,
    claim_vector,
    item,
)
from joshua_core.jobs.insights.gate import (
    DISMISSED_BEFORE,
    LOW_CONFIDENCE,
    InsightGate,
    cap,
    parse_day,
    we_do_parts,
)
from joshua_core.jobs.insights.knowledge import published_pages
from joshua_core.jobs.insights.page import Insight, InsightFile
from joshua_core.jobs.insights.worker import RaisedInsight
from joshua_core.jobs.research.text import Part

PARTS = [
    Part(IRRIGATION, "The controller runs every zone on the 20-minute summer program, at 6 am."),
    Part(LOG, "Cool and dry. The irrigation still runs the summer program every morning."),
    Part(f"knowledge/{AUTUMN}", "Cut each zone to half."),
    Part("insights/garden/x.md", "An insight about the drip line."),
]


async def _embed(text: str) -> list[list[float]]:
    return [claim_vector(text)]


async def _no_model(text: str) -> list[list[float]]:
    raise RuntimeError("no model")


def _file(claim: str, status: str = "new") -> InsightFile:
    insight = Insight(
        topic=IRRIGATION,
        section="garden",
        claim=claim,
        status=status,  # type: ignore[arg-type]
        valid_from=date(2026, 10, 1),
        valid_until=date(2026, 11, 30),
        found_at=date(2026, 10, 1),
        seen_at=date(2026, 10, 1),
    )
    return InsightFile(f"garden/{status}", Path(f"/x/{status}.md"), insight, "")


def _gate(tmp_path: Path, *, embed: Any = _embed, opened=(), dismissed=()) -> InsightGate:
    data = tmp_path / "data"
    build_insights_wiki(data)
    return InsightGate(
        parts=we_do_parts(PARTS),
        known=published_pages(IRRIGATION, data),
        open_insights=list(opened),
        dismissed=list(dismissed),
        today=TODAY,
        default_valid_days=60,
        dup_sim=0.88,
        embed=embed,
    )


async def _reason(tmp_path: Path, **overrides: Any) -> str:
    verdict = await _gate(tmp_path).check(RaisedInsight.model_validate(item(**overrides)))
    assert verdict.kind == "rejected"
    return verdict.reason


def test_we_do_parts_leave_out_knowledge_and_insights() -> None:
    assert [p.path for p in we_do_parts(PARTS)] == [IRRIGATION, LOG]


async def test_a_good_insight_passes(tmp_path: Path) -> None:
    verdict = await _gate(tmp_path).check(RaisedInsight.model_validate(item()))
    assert verdict.kind == "new" and verdict.checked is not None
    assert verdict.checked.phrase_paths == {PHRASE: IRRIGATION}
    assert verdict.checked.valid_until == date(2026, 11, 30)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"we_do": ["every zone on the 30-minute summer program"]}, "is not in the bundle"),
        ({"we_do": []}, "we_do has no phrase"),
        ({"we_do": ["ab"]}, "has 2 characters"),
        ({"we_do": ["Cut each zone to half"]}, "is not in the bundle"),
        ({"we_do_paths": [f"knowledge/{AUTUMN}"]}, "not a bundle path"),
        ({"we_do_paths": [LOG]}, "holds none of the we_do phrases"),
        ({"knowledge_says": []}, "knowledge_says has no page"),
        ({"knowledge_says": [DRAFT]}, "not a published knowledge page of this topic"),
        ({"knowledge_says": [LAWN_PAGE]}, "not a published knowledge page of this topic"),
        ({"knowledge_says": ["../../people/alex.md"]}, "not a published knowledge page"),
        ({"confidence": "low"}, LOW_CONFIDENCE),
        ({"valid_from": "2026-11-30", "valid_until": "2026-11-01"}, "not after valid_from"),
        ({"valid_from": "2026-09-01", "valid_until": "2026-10-01"}, "before today"),
        ({"valid_until": "soon"}, "valid_until is not an ISO day"),
        ({"valid_from": "13/10/2026"}, "valid_from is not an ISO day"),
        ({"action": "  "}, "the action is empty"),
        ({"claim": "Too short."}, "the claim has 10 characters"),
        ({"claim": "x" * 201}, "the claim has 201 characters"),
    ],
)
async def test_a_rejection_has_its_reason(
    tmp_path: Path, overrides: dict[str, Any], reason: str
) -> None:
    assert reason in await _reason(tmp_path, **overrides)


async def test_missing_days_take_the_defaults(tmp_path: Path) -> None:
    verdict = await _gate(tmp_path).check(
        RaisedInsight.model_validate(item(valid_from=None, valid_until=None))
    )
    assert verdict.checked is not None
    assert verdict.checked.valid_from == TODAY
    assert (verdict.checked.valid_until - TODAY).days == 60


async def test_empty_we_do_paths_come_from_the_phrases(tmp_path: Path) -> None:
    verdict = await _gate(tmp_path).check(
        RaisedInsight.model_validate(item(we_do=[PHRASE, LOG_PHRASE], we_do_paths=[]))
    )
    assert verdict.checked is not None and verdict.checked.we_do_paths == [IRRIGATION, LOG]


async def test_a_knowledge_prefix_is_accepted(tmp_path: Path) -> None:
    verdict = await _gate(tmp_path).check(
        RaisedInsight.model_validate(item(knowledge_says=[f"knowledge/{AUTUMN}"]))
    )
    assert verdict.checked is not None and verdict.checked.knowledge_says == [AUTUMN]


async def test_a_duplicate_of_an_open_insight_is_a_refresh(tmp_path: Path) -> None:
    opened = _file("The garden runs the summer program in October.", "told")
    verdict = await _gate(tmp_path, opened=[opened]).check(RaisedInsight.model_validate(item()))
    assert verdict.kind == "refresh" and verdict.target is opened


async def test_the_same_text_is_a_duplicate_with_no_model(tmp_path: Path) -> None:
    gate = _gate(tmp_path, embed=_no_model, opened=[_file(CLAIM.upper() + "  ")])
    verdict = await gate.check(RaisedInsight.model_validate(item()))
    assert verdict.kind == "refresh"
    other = await gate.check(RaisedInsight.model_validate(item(claim="Mulch the roses bed now.")))
    assert other.kind == "new"
    assert any("no embedding model" in n for n in gate.notes)


async def test_a_duplicate_of_a_dismissed_insight_is_dropped(tmp_path: Path) -> None:
    gate = _gate(tmp_path, dismissed=[_file("Summer program, still, in October.", "dismissed")])
    verdict = await gate.check(RaisedInsight.model_validate(item()))
    assert verdict.kind == "rejected" and verdict.reason == DISMISSED_BEFORE


async def test_a_second_copy_in_one_run_is_rejected(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    first = await gate.check(RaisedInsight.model_validate(item()))
    second = await gate.check(RaisedInsight.model_validate(item()))
    assert first.kind == "new" and second.kind == "rejected"
    assert "another insight of this run" in second.reason


async def test_cap_keeps_severity_then_confidence(tmp_path: Path) -> None:
    gate = _gate(tmp_path)
    raised = [
        item(claim="A note about the drip line and the roses.", severity="note"),
        item(claim="Mulch the roses bed before the frost.", severity="act", confidence="medium"),
        item(claim="Frost is near and the summer program runs.", severity="urgent"),
        item(claim="The lawn and the drip line need a check.", severity="act"),
    ]
    verdicts = [await gate.check(RaisedInsight.model_validate(r)) for r in raised]
    keep, over = cap(verdicts, 3)
    assert [v.checked.severity for v in keep] == ["urgent", "act", "act"]  # type: ignore[union-attr]
    assert keep[1].checked.confidence == "high"  # type: ignore[union-attr]
    assert [v.claim for v in over] == ["A note about the drip line and the roses."]


def test_parse_day() -> None:
    assert parse_day(None, "x") == (None, "")
    assert parse_day(" 2026-10-01 ", "x") == (date(2026, 10, 1), "")
    assert parse_day("2026-10-01T00:00:00Z", "x") == (date(2026, 10, 1), "")
    assert parse_day("nope", "x")[1].startswith("x is not")
