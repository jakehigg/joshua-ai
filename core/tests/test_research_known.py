"""Knowledge first: the search for published knowledge pages, and the `apply`
worker before any lookup of the crew. Fake workers, a fake store, and a fake
embedder; no model, no network, no database."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from joshua_core.engine.usage import UsageLedger
from joshua_core.jobs.research import crew
from joshua_core.jobs.research.known import (
    KnowledgeSearch,
    KnownPage,
    find_known,
    known_page,
    lowest_confidence,
)
from joshua_core.jobs.research.text import Grounding
from joshua_core.jobs.research.write import render_page
from joshua_shared import frontmatter, layout
from research_fakes import (
    APPLY_USAGE,
    LOCAL,
    PRIMARY,
    SECONDARY,
    FakeInternet,
    FakeStore,
    FakeWorkers,
    research_settings,
    word_vector,
)

SETTINGS = research_settings()
CFG = SETTINGS.jobs.research
GENERAL = "How often should a snake plant in a gritty mix be watered in winter?"
GROUNDINGS = [Grounding("gritty soil mix", "houseplants/snake-plant.md", "A gritty soil mix.")]
# A published page of another topic, and one that is not published.
OTHER = "knowledge/houseplants/zz-plant/winter-watering.md"
DRAFTY = "knowledge/houseplants/zz-plant/draft.md"
HIGH = "knowledge/houseplants/aloe/gritty-mix.md"
OTHER_SOURCES = [
    {"url": PRIMARY, "title": "Extension", "trust": "primary", "retrieved": date(2026, 9, 1)},
    {"url": SECONDARY, "title": "Guide", "trust": "secondary", "retrieved": date(2026, 9, 2)},
]


def _page(
    root: Path,
    rel: str,
    *,
    status: str = "published",
    confidence: str = "medium",
    topic: str = "houseplants/zz-plant.md",
    sources: list[dict[str, Any]] | None = None,
) -> None:
    path = layout.wiki_root(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# How often in winter?\n\n## Answer\n\nEvery 4 to 6 weeks in a mineral mix.\n"
    path.write_text(
        frontmatter.update(
            body,
            question="How often should a plant in a gritty mix be watered in winter?",
            answer_short="Every 4 to 6 weeks in a mineral mix.",
            topic=topic,
            confidence=confidence,
            status=status,
            sources=OTHER_SOURCES if sources is None else sources,
        ),
        encoding="utf-8",
    )


async def _embed(text: str) -> list[list[float]]:
    return [word_vector(text)]


def _search(root: Path, near: dict[str, float], **kwargs: Any) -> KnowledgeSearch:
    return KnowledgeSearch(
        store=FakeStore(near),
        embed=kwargs.pop("embed", _embed),
        data_root=root,
        min_sim=kwargs.pop("min_sim", CFG.apply_sim),
        limit=kwargs.pop("limit", CFG.apply_pages),
    )


def test_the_defaults() -> None:
    assert (CFG.apply_sim, CFG.apply_pages) == (0.70, 5)


# -- the search --------------------------------------------------------------------


async def test_the_search_gives_published_knowledge_pages_only(tmp_path: Path) -> None:
    _page(tmp_path, OTHER)
    _page(tmp_path, DRAFTY, status="held")
    _page(tmp_path, HIGH, confidence="high", topic="houseplants/aloe.md")
    dossier = layout.knowledge_dossier("houseplants/zz-plant.md", tmp_path)
    dossier.write_text("---\ntopic: houseplants/zz-plant.md\nquestions: []\n---\n")
    hidden = layout.wiki_root(tmp_path) / "knowledge/.trash/x.md"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("---\nquestion: x\nstatus: published\n---\n")
    near = {
        f"wiki/{OTHER}": 0.80,
        f"wiki/{DRAFTY}": 0.95,
        f"wiki/{HIGH}": 0.90,
        "wiki/knowledge/houseplants/zz-plant.md": 0.99,
        "wiki/knowledge/.trash/x.md": 0.99,
        "wiki/houseplants/zz-plant.md": 0.99,
        "wiki/knowledge/../people/alex.md": 0.99,
        f"wiki/{OTHER}.gone": 0.99,
        "people/alex/x.md": 0.99,
        "wiki/knowledge/houseplants/far.md": 0.5,
    }
    search = _search(tmp_path, near)
    pages = await find_known(GENERAL, search)
    assert [p.path for p in pages] == [HIGH, OTHER]
    assert search.store.calls == [{"kinds": ["knowledge"], "min_sim": 0.70}]  # type: ignore[attr-defined]
    assert pages[1].topic == "houseplants/zz-plant.md"
    assert pages[1].answer_short == "Every 4 to 6 weeks in a mineral mix."
    assert "## Answer" in pages[1].text
    assert [p.path for p in await find_known(GENERAL, _search(tmp_path, near, limit=1))] == [HIGH]


async def test_a_search_with_no_vectors_or_no_index_gives_nothing(tmp_path: Path) -> None:
    async def broken(text: str) -> list[list[float]]:
        raise RuntimeError("no model")

    async def empty(text: str) -> list[list[float]]:
        return []

    search = _search(tmp_path, {}, embed=broken)
    assert await find_known(GENERAL, search) == []
    assert "no embedding model" in search.notes[0]
    assert await find_known(GENERAL, _search(tmp_path, {}, embed=empty)) == []

    class Broken(FakeStore):
        async def kb_near_paths(self, *args: Any, **kwargs: Any) -> dict[str, float]:
            raise RuntimeError("no database")

    search = _search(tmp_path, {})
    search.store = Broken()
    assert await find_known(GENERAL, search) == []
    assert search.notes == ["The search for knowledge we hold did not run."]


def test_known_page_and_the_lowest_confidence(tmp_path: Path) -> None:
    _page(tmp_path, OTHER, sources=[{"url": PRIMARY, "trust": "primary"}, {"title": "no url"}])
    page = known_page(f"wiki/{OTHER}", tmp_path)
    assert page is not None and [s["url"] for s in page.sources] == [PRIMARY]
    pages = [
        KnownPage("a", "t", "q", "s", "high"),
        KnownPage("b", "t", "q", "s", "medium"),
    ]
    assert lowest_confidence(pages) == "medium"
    assert lowest_confidence([KnownPage("a", "t", "q", "s", "bogus")]) == "low"
    assert lowest_confidence([]) == "low"


# -- the crew ----------------------------------------------------------------------


async def _crew(
    root: Path,
    workers: FakeWorkers,
    net: FakeInternet,
    near: dict[str, float],
    *,
    ledger: UsageLedger | None = None,
) -> crew.CrewResult:
    def floor(text: str) -> str:
        return "the question asks about us" if " our " in f" {text} " else ""

    return await crew.research_question(
        GENERAL,
        GROUNDINGS,
        internet_settings=SETTINGS.internet,
        research_settings=CFG,
        editor_model=None,
        ledger=ledger or UsageLedger(),
        internet_runner=net,
        editor_runner=workers,
        local_context=LOCAL,
        knowledge=_search(root, near),
        floor=floor,
    )


def _apply(verdict: str, cites: list[str], narrowed: str = "") -> dict[str, Any]:
    return {
        "verdict": verdict,
        "answer_short": "Every 4 to 6 weeks in winter.",
        "answer": "Water every 4 to 6 weeks from October to March.",
        "local_notes": "The hall is cool, so the longer end fits.",
        "cites": cites,
        "narrowed_question": narrowed,
    }


async def test_an_answered_verdict_writes_the_page_with_no_lookup(tmp_path: Path) -> None:
    _page(tmp_path, OTHER, confidence="high")
    _page(tmp_path, HIGH, confidence="medium", topic="houseplants/aloe.md")
    workers = FakeWorkers(apply_answer=_apply("answered", [OTHER, HIGH, "knowledge/made-up.md"]))
    net = FakeInternet()
    ledger = UsageLedger()
    near = {f"wiki/{OTHER}": 0.9, f"wiki/{HIGH}": 0.8}
    result = await _crew(tmp_path, workers, net, near, ledger=ledger)
    assert net.texts == []
    assert workers.names() == ["apply"]
    assert result.publish and result.mode == crew.MODE_APPLIED
    assert result.applies == [OTHER, HIGH]
    assert result.page is not None
    assert result.page.confidence == "medium"
    assert [(s.url, s.trust) for s in result.page.sources] == [
        (PRIMARY, "primary"),
        (SECONDARY, "secondary"),
    ]
    assert result.usage[crew.APPLY] == APPLY_USAGE
    assert ledger.as_data()["apply"]["calls"] == 1
    apply_text = workers.calls[0][1]
    assert GENERAL in apply_text and LOCAL in apply_text
    assert f'<page path="{OTHER}">' in apply_text and "## Answer" in apply_text


async def test_a_cite_that_is_not_a_published_knowledge_page_is_dropped(tmp_path: Path) -> None:
    _page(tmp_path, OTHER)
    _page(tmp_path, DRAFTY, status="held")
    workers = FakeWorkers(apply_answer=_apply("answered", [DRAFTY, OTHER, "houseplants/x.md"]))
    near = {f"wiki/{OTHER}": 0.9, f"wiki/{DRAFTY}": 0.95}
    result = await _crew(tmp_path, workers, FakeInternet(), near)
    assert result.mode == crew.MODE_APPLIED
    assert result.applies == [OTHER]


async def test_an_answer_with_no_cite_left_runs_the_full_crew(tmp_path: Path) -> None:
    _page(tmp_path, OTHER)
    workers = FakeWorkers(apply_answer=_apply("answered", ["knowledge/made-up.md"]))
    net = FakeInternet()
    result = await _crew(tmp_path, workers, net, {f"wiki/{OTHER}": 0.9})
    assert result.mode == crew.MODE_FULL and result.applies == []
    assert len(net.texts) == 2 and net.texts[0].startswith(GENERAL)
    assert "<known_answers>" not in workers.calls[-1][1]


async def test_a_partial_verdict_looks_up_the_narrowed_question(tmp_path: Path) -> None:
    _page(tmp_path, OTHER)
    narrowed = "Does a snake plant in a gritty mix need any feed from October to March?"
    workers = FakeWorkers(apply_answer=_apply("partial", [OTHER], narrowed))
    net = FakeInternet()
    result = await _crew(tmp_path, workers, net, {f"wiki/{OTHER}": 0.9})
    assert result.publish and result.mode == crew.MODE_NARROWED
    assert result.applies == [OTHER] and result.narrowed_question == narrowed
    assert net.texts[0].startswith(narrowed) and narrowed in net.texts[1]
    assert all(GENERAL not in text for text in net.texts)
    assert workers.names() == ["apply", "editor"]
    editor_text = workers.calls[1][1]
    assert f"<question>\n{GENERAL}\n</question>" in editor_text
    assert f"- {OTHER}: Every 4 to 6 weeks in a mineral mix." in editor_text
    assert f"The lookups asked only: {narrowed}" in editor_text


@pytest.mark.parametrize(
    "narrowed", ["", "How often should our snake plant get feed from October to March?"]
)
async def test_a_narrowed_question_that_fails_the_floor_is_not_sent(
    tmp_path: Path, narrowed: str
) -> None:
    _page(tmp_path, OTHER)
    workers = FakeWorkers(apply_answer=_apply("partial", [OTHER], narrowed))
    net = FakeInternet()
    result = await _crew(tmp_path, workers, net, {f"wiki/{OTHER}": 0.9})
    assert result.mode == crew.MODE_FULL and result.applies == [OTHER]
    assert net.texts[0].startswith(GENERAL)
    assert all("our snake plant" not in text for text in net.texts)
    assert "the narrowed question was not sent" in result.notes[0]
    assert "<known_answers>" in workers.calls[-1][1]


async def test_a_none_verdict_runs_the_full_crew(tmp_path: Path) -> None:
    _page(tmp_path, OTHER)
    workers = FakeWorkers()
    net = FakeInternet()
    result = await _crew(tmp_path, workers, net, {f"wiki/{OTHER}": 0.9})
    assert result.publish and result.mode == crew.MODE_FULL and result.applies == []
    assert workers.names() == ["apply", "editor"] and len(net.texts) == 2
    assert result.usage[crew.APPLY] == APPLY_USAGE


async def test_no_answer_from_the_apply_worker_runs_the_full_crew(tmp_path: Path) -> None:
    _page(tmp_path, OTHER)
    workers = FakeWorkers(apply_answer={"verdict": "maybe"})
    net = FakeInternet()
    result = await _crew(tmp_path, workers, net, {f"wiki/{OTHER}": 0.9})
    assert result.publish and result.mode == crew.MODE_FULL
    assert result.notes == ["no answer from the apply worker; the full crew ran"]


async def test_no_hit_makes_no_apply_call(tmp_path: Path) -> None:
    workers = FakeWorkers()
    net = FakeInternet()
    result = await _crew(tmp_path, workers, net, {})
    assert workers.names() == ["editor"] and crew.APPLY not in result.usage


async def test_an_applied_page_of_low_confidence_is_held(tmp_path: Path) -> None:
    _page(tmp_path, OTHER, confidence="low")
    workers = FakeWorkers(apply_answer=_apply("answered", [OTHER]))
    result = await _crew(tmp_path, workers, FakeInternet(), {f"wiki/{OTHER}": 0.9})
    assert result.mode == crew.MODE_APPLIED and not result.publish
    assert result.hold_reason == "the confidence is low"


async def test_kept_lookups_skip_the_search(tmp_path: Path) -> None:
    from joshua_core.engine.internet import InternetAnswer
    from research_fakes import lookup

    _page(tmp_path, OTHER)
    workers = FakeWorkers(apply_answer=_apply("answered", [OTHER]))
    search = _search(tmp_path, {f"wiki/{OTHER}": 0.9})
    result = await crew.research_question(
        GENERAL,
        GROUNDINGS,
        internet_settings=SETTINGS.internet,
        research_settings=CFG,
        editor_model=None,
        ledger=UsageLedger(),
        editor_runner=workers,
        draft=InternetAnswer.model_validate(lookup(PRIMARY)),
        challenge=InternetAnswer.model_validate(lookup(SECONDARY)),
        knowledge=search,
    )
    assert workers.names() == ["editor"] and result.mode == crew.MODE_FULL
    assert search.store.calls == []  # type: ignore[attr-defined]


# -- the page ----------------------------------------------------------------------


def test_an_applied_page_names_the_pages_it_uses(tmp_path: Path) -> None:
    _page(tmp_path, OTHER)
    page = known_page(f"wiki/{OTHER}", tmp_path)
    assert page is not None
    answer = crew.applied_page(crew.ApplyAnswer.model_validate(_apply("answered", [OTHER])), [page])
    text = render_page(
        question=GENERAL,
        topic="houseplants/snake-plant.md",
        context=["houseplants/snake-plant.md"],
        grounded_in=["gritty soil mix"],
        answer=answer,
        day=date(2026, 10, 19),
        refresh_days=180,
        run_id=None,
        local_context=LOCAL,
        applies=[OTHER],
        self_path="knowledge/houseplants/snake-plant/winter.md",
        retrieved={PRIMARY: date(2026, 9, 1)},
    )
    meta, body = frontmatter.split(text)
    assert meta["applies"] == [OTHER] and meta["local_context"] == LOCAL
    assert [s["retrieved"] for s in meta["sources"]] == [date(2026, 9, 1), date(2026, 10, 19)]
    assert body.rstrip().endswith(f"Applied from: [{OTHER}](../zz-plant/winter-watering.md)")
    assert "## Where this differs for us\n\nThe hall is cool" in body
