"""The insight page, the table of every insight, expiry, the published
knowledge pages of a topic, and the `insight` worker."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from insights_fakes import (
    AUTUMN,
    CLAIM,
    DRAFT,
    IRRIGATION,
    LAWN_PAGE,
    PHRASE,
    TODAY,
    FakeInsightWorker,
    answer,
    build_insights_wiki,
    item,
)
from joshua_core.engine.usage import Usage
from joshua_core.jobs.insights.knowledge import normalize_page, published_pages
from joshua_core.jobs.insights.page import (
    HEADINGS,
    Body,
    Insight,
    body_sections,
    due_to_expire,
    expire,
    index_order,
    insight_path,
    insight_section,
    load_insight,
    load_insights,
    parse_insight,
    render_index,
    render_insight,
    set_fields,
    updated_text,
    write_index,
    write_text,
)
from joshua_core.jobs.insights.worker import InsightAnswer, insight_text, raise_insights
from joshua_core.jobs.research.text import Part
from joshua_shared import frontmatter, layout


def _insight(claim: str = CLAIM, **overrides: object) -> Insight:
    values: dict[str, object] = {
        "topic": IRRIGATION,
        "section": "garden",
        "claim": claim,
        "severity": "act",
        "valid_from": date(2026, 10, 1),
        "valid_until": date(2026, 11, 30),
        "we_do": [PHRASE],
        "we_do_paths": [IRRIGATION],
        "knowledge_says": [AUTUMN],
        "confidence": "high",
        "found_at": TODAY,
        "seen_at": TODAY,
    }
    values.update(overrides)
    return Insight.model_validate(values)


BODY = Body(
    why="Roots rot. [x](https://evil.example.com)",
    action="Set 10 minutes.\n# Not a heading",
    evidence="See [ours](/garden/irrigation.md) and ![p](garden/irrigation.md).",
    phrase_paths={PHRASE: IRRIGATION},
    known={AUTUMN: "Cut each zone to half."},
)


def _save(data: Path, insight_id: str, insight: Insight) -> Path:
    return write_text(
        insight_path(insight_id, data), render_insight(insight_id, insight, BODY), data
    )


def test_insight_section() -> None:
    assert insight_section("garden/irrigation.md") == "garden"
    assert insight_section("roses.md") == "general"
    assert insight_section("Home Lab/nas.md") == "home-lab"
    assert insight_section("___/x.md") == "general"


def test_insight_path_refuses_a_bad_id(tmp_path: Path) -> None:
    for bad in ("garden", "a/b/c", "../x", "garden/../x", "Garden/x", ""):
        with pytest.raises(ValueError):
            insight_path(bad, tmp_path)


def test_render_and_parse(tmp_path: Path) -> None:
    text = render_insight("garden/summer", _insight(), BODY)
    meta, body = frontmatter.split(text)
    assert list(meta)[:3] == ["topic", "section", "claim"]
    assert "told_at" not in meta and meta["source"] == "insights"
    assert parse_insight(text) == _insight()
    sections = body_sections(text)
    assert list(sections) == list(HEADINGS)
    assert "evil.example.com" not in sections["Why it matters"]
    assert sections["What to do"] == "Set 10 minutes.\nNot a heading"
    assert "[ours](../../garden/irrigation.md)" in sections["Evidence"]
    assert "![p]" not in sections["Evidence"]
    assert "Cut each zone to half." in sections["What is known"]
    assert parse_insight("# no frontmatter\n") is None
    assert parse_insight("---\ntopic: x\nclaim: y\nstatus: bogus\n---\n") is None


def test_load_and_set_fields(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _save(data, "garden/summer", _insight())
    (layout.insights_root(data) / "garden" / "junk.md").write_text("no frontmatter\n")
    (layout.insights_root(data) / ".hidden").mkdir()
    write_index(data)
    [found] = load_insights(data)
    assert found.id == "garden/summer"
    assert load_insights(data, "garden/roses.md") == []
    assert load_insight("garden/nope", data) is None
    assert load_insight("bad", data) is None
    changed = set_fields(found, data, status="acknowledged", resolved_by="alex")
    assert changed.insight.status == "acknowledged"
    assert frontmatter.split(changed.text)[1] == frontmatter.split(found.text)[1]
    with pytest.raises(ValueError):
        updated_text(found, status="bogus")


def test_write_text_refuses_a_path_outside_insights(tmp_path: Path) -> None:
    data = tmp_path / "data"
    for path in (
        layout.knowledge_root(data) / "x.md",
        layout.wiki_root(data) / "garden" / "x.md",
        layout.insights_root(data) / ".." / "x.md",
    ):
        with pytest.raises(ValueError):
            write_text(path, "x", data)


def test_the_index_puts_open_insights_first_and_newest_first(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _save(
        data,
        "garden/old-open",
        _insight("An open insight found long ago.", found_at=date(2026, 9, 1)),
    )
    _save(data, "garden/new-open", _insight("An open insight found today here."))
    _save(data, "garden/closed", _insight("A dismissed insight, found today.", status="dismissed"))
    _save(
        data,
        "general/told",
        _insight("A told insight found yesterday.", status="told", found_at=date(2026, 10, 18)),
    )
    order = [f.id for f in index_order(load_insights(data))]
    assert order == ["garden/new-open", "general/told", "garden/old-open", "garden/closed"]
    text = layout.insights_index(data).read_text() if write_index(data) else ""
    rows = [line for line in text.splitlines() if line.startswith("| ") and "---" not in line][1:]
    assert [r.split(" | ")[0] for r in rows] == ["| new", "| told", "| new", "| dismissed"]
    assert "[garden/irrigation.md](../garden/irrigation.md)" in rows[0]
    assert "(garden/new-open.md)" in rows[0]
    assert "| none |" in render_index([])


def test_expire_marks_only_open_insights_past_valid_until(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _save(
        data,
        "garden/past",
        _insight("Past its window, still open.", valid_until=date(2026, 10, 18)),
    )
    _save(data, "garden/today", _insight("Valid until today, still open.", valid_until=TODAY))
    _save(
        data,
        "garden/done",
        _insight("Past and dismissed already.", valid_until=date(2026, 10, 2), status="dismissed"),
    )
    assert [f.id for f in due_to_expire(load_insights(data), TODAY)] == ["garden/past"]
    due, written = expire(data, IRRIGATION, TODAY, dry_run=True)
    assert [f.id for f in due] == ["garden/past"] and written == []
    assert load_insight("garden/past", data).insight.status == "new"  # type: ignore[union-attr]
    done, written = expire(data, IRRIGATION, TODAY, dry_run=False)
    gone = load_insight("garden/past", data)
    assert gone is not None and gone.insight.status == "expired"
    assert gone.insight.resolved_at == TODAY and gone.insight.resolved_by == "expiry"
    assert layout.insights_index(data) in written
    assert expire(data, IRRIGATION, TODAY, dry_run=False) == ([], [])


# -- knowledge ---------------------------------------------------------------------


def test_published_pages(tmp_path: Path) -> None:
    data = tmp_path / "data"
    build_insights_wiki(data)
    pages = published_pages(IRRIGATION, data)
    assert list(pages) == [AUTUMN]
    assert pages[AUTUMN].answer_short.startswith("Cut each zone")
    assert DRAFT not in pages and LAWN_PAGE not in pages
    extra = {
        "garden/irrigation/new-page.md": "---\nstatus: published\n---\n# New\n",
        "garden/lawn/x.md": "---\nstatus: published\n---\n# Other topic\n",
        "garden/irrigation/../x.md": "---\nstatus: published\n---\n",
    }
    assert list(published_pages(IRRIGATION, data, extra)) == [
        AUTUMN,
        "garden/irrigation/new-page.md",
    ]
    assert published_pages("garden/roses.md", data) == {}


def test_a_dossier_page_outside_knowledge_is_not_read(tmp_path: Path) -> None:
    data = tmp_path / "data"
    build_insights_wiki(data)
    dossier = layout.knowledge_dossier(IRRIGATION, data)
    dossier.write_text(dossier.read_text().replace(AUTUMN, "../../people/alex.md"))
    assert list(published_pages(IRRIGATION, data)) == [AUTUMN]


def test_normalize_page() -> None:
    assert normalize_page(f" /knowledge/{AUTUMN} ") == AUTUMN
    assert normalize_page(AUTUMN) == AUTUMN


# -- the worker --------------------------------------------------------------------


def test_the_schema_is_the_strict_one() -> None:
    from joshua_core.jobs.insights.worker import _Loose

    assert _Loose.model_json_schema() == InsightAnswer.model_json_schema()
    schema = InsightAnswer.model_json_schema()
    assert "severity" in str(schema) and "urgent" in str(schema)


def test_insight_text() -> None:
    text = insight_text(
        topic=IRRIGATION,
        title="Irrigation",
        parts=[Part(IRRIGATION, "Our page.")],
        known=[],
        open_insights=[],
        today=TODAY,
        timezone="UTC",
        instance="Home",
    )
    assert "Today: 2026-10-19 (Monday, October)" in text
    assert "<open_insights>\n- none\n</open_insights>" in text
    assert f"## {IRRIGATION}\n\nOur page." in text


async def test_raise_insights_keeps_good_items_and_rejects_bad_ones() -> None:
    worker = FakeInsightWorker(answer(item(), {"claim": "Missing fields here."}, "not a dict"))
    used: list[Usage] = []
    raised = await raise_insights(user_text="x", model="m", runner=worker, usage_out=used)
    assert raised.candidates is not None and len(raised.candidates) == 1
    assert [claim for claim, _ in raised.invalid] == ["Missing fields here.", "(no claim)"]
    assert used and worker.kwargs[0]["timeout_s"] == 120.0


async def test_raise_insights_with_no_answer() -> None:
    raised = await raise_insights(
        user_text="x", model=None, runner=FakeInsightWorker({"insights": 3})
    )
    assert raised.candidates is None
