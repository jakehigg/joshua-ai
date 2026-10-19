"""The crew of one question and the checks after the editor, with fake workers."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from joshua_core.engine.types import WorkerStats
from joshua_core.engine.usage import UsageLedger
from joshua_core.jobs.research import crew
from joshua_core.jobs.research.crew import (
    EditorAnswer,
    challenge_text,
    check_editor,
    domain_matches,
    on_list,
    registrable_domain,
)
from joshua_core.jobs.research.text import Grounding
from joshua_core.jobs.research.write import PageTooLong, render_page
from joshua_shared import frontmatter
from research_fakes import (
    FORUM_A,
    FORUM_B,
    GOOD_EDITOR,
    GROUNDED,
    PRIMARY,
    SECOND_PAGE,
    SECONDARY,
    UNGIVEN,
    FakeInternet,
    FakeWorkers,
    editor,
    lookup,
    research_settings,
)

SETTINGS = research_settings()
CFG = SETTINGS.jobs.research
GIVEN = [PRIMARY, SECONDARY, SECOND_PAGE, FORUM_A, FORUM_B]
GROUNDINGS = [Grounding("gritty soil mix", "houseplants/snake-plant.md", "A gritty soil mix.")]


def _answer(*sources: tuple[str, str], **overrides: Any) -> EditorAnswer:
    return EditorAnswer.model_validate(editor(*sources, **overrides))


# -- domains -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "entry", "match"),
    [
        ("extension.example.edu", ".edu", True),
        ("example.edu", ".edu", True),
        ("example.education", ".edu", False),
        ("extension.example.edu", "extension.", True),
        ("www.extension.example.edu", "extension.", True),
        ("myextension.example", "extension.", False),
        ("forum.example", "forum.example", True),
        ("board.forum.example", "forum.example", True),
        ("notforum.example", "forum.example", False),
        ("www.reddit.com", "reddit.com", True),
        ("", ".edu", False),
    ],
)
def test_a_domain_entry_matches_a_host(host: str, entry: str, match: bool) -> None:
    assert domain_matches(host, entry) is match


@pytest.mark.parametrize(
    ("value", "domain"),
    [
        ("https://www.example.org/a", "example.org"),
        ("https://a.b.example.org/a", "example.org"),
        ("shop.example.co.uk", "example.co.uk"),
        ("dept.example.ac.uk", "example.ac.uk"),
        ("example.com.au", "example.com.au"),
        ("a.example.uk", "example.uk"),
        ("localhost", "localhost"),
    ],
)
def test_the_registrable_domain(value: str, domain: str) -> None:
    assert registrable_domain(value) == domain


def test_on_list_reads_the_host_of_a_url() -> None:
    assert on_list(FORUM_B, ["forum.example"])
    assert not on_list("not a url", ["forum.example"])


# -- the checks ------------------------------------------------------------------


def test_a_good_pair_publishes() -> None:
    result = check_editor(_answer((PRIMARY, "primary"), (SECONDARY, "secondary")), GIVEN, CFG)
    assert result.publish and result.holds == []


def test_one_source_holds() -> None:
    result = check_editor(_answer((PRIMARY, "primary")), GIVEN, CFG)
    assert not result.publish
    assert "1 independent sources" in result.hold_reason


def test_two_sources_on_one_domain_hold() -> None:
    result = check_editor(_answer((SECONDARY, "primary"), (SECOND_PAGE, "secondary")), GIVEN, CFG)
    assert "1 independent sources" in result.hold_reason


def test_a_distrusted_pair_holds_as_weak() -> None:
    result = check_editor(_answer((FORUM_A, "primary"), (FORUM_B, "primary")), GIVEN, CFG)
    assert [s.trust for s in result.page.sources] == ["weak", "weak"]  # type: ignore[union-attr]
    assert "0 independent sources" in result.hold_reason


def test_a_trusted_source_is_not_weak() -> None:
    result = check_editor(_answer((PRIMARY, "weak"), (SECONDARY, "secondary")), GIVEN, CFG)
    assert result.page is not None and result.page.sources[0].trust == "secondary"
    assert result.publish


def test_a_url_no_lookup_returned_is_dropped_and_counted_again() -> None:
    result = check_editor(_answer((PRIMARY, "primary"), (UNGIVEN, "primary")), GIVEN, CFG)
    assert result.dropped_urls == [UNGIVEN]
    assert [s.url for s in result.page.sources] == [PRIMARY]  # type: ignore[union-attr]
    assert not result.publish


def test_a_repeated_source_counts_once() -> None:
    answer = _answer((PRIMARY, "primary"), (PRIMARY + "/", "primary"))
    result = check_editor(answer, GIVEN, CFG)
    assert len(result.page.sources) == 1  # type: ignore[union-attr]


def test_verdict_hold_and_low_confidence_hold() -> None:
    pair = ((PRIMARY, "primary"), (SECONDARY, "secondary"))
    held = check_editor(_answer(*pair, verdict="hold", reason="overturned"), GIVEN, CFG)
    assert held.hold_reason == "the editor held it: overturned"
    low = check_editor(_answer(*pair, confidence="low"), GIVEN, CFG)
    assert low.hold_reason == "the confidence is low"


# -- the crew --------------------------------------------------------------------


async def _crew(internet_fake: FakeInternet, workers: FakeWorkers) -> crew.CrewResult:
    return await crew.research_question(
        GROUNDED,
        GROUNDINGS,
        internet_settings=SETTINGS.internet,
        research_settings=CFG,
        editor_model=None,
        ledger=UsageLedger(),
        internet_runner=internet_fake,
        editor_runner=workers,
    )


async def test_the_crew_runs_the_three_steps() -> None:
    net = FakeInternet()
    workers = FakeWorkers()
    result = await _crew(net, workers)
    assert result.publish
    assert len(net.texts) == 2
    assert net.texts[0].startswith(GROUNDED) and "primary source" in net.texts[0]
    assert net.texts[1].startswith("You check an answer") and GROUNDED in net.texts[1]
    editor_text = workers.calls[0][1]
    assert '"gritty soil mix" (houseplants/snake-plant.md): A gritty soil mix.' in editor_text
    assert "Distrusted domains: forum.example" in editor_text


@pytest.mark.parametrize("step", ["draft", "challenge", "editor"])
async def test_no_answer_at_any_step_holds(step: str) -> None:
    net = FakeInternet()
    workers = FakeWorkers()
    if step == "draft":
        net.draft = None
    elif step == "challenge":
        net.challenge = None
    else:
        workers.editor_answer = {"bad": "shape"}
    result = await _crew(net, workers)
    assert result.page is None and not result.publish
    assert result.hold_reason.startswith("no answer")


async def test_the_lookups_take_the_research_limits() -> None:
    cfg = research_settings(
        "    lookup_timeout_seconds: 450\n    lookup_max_turns: 25\n"
    ).jobs.research
    net = FakeInternet()
    await crew.research_question(
        GROUNDED,
        GROUNDINGS,
        internet_settings=SETTINGS.internet,
        research_settings=cfg,
        editor_model=None,
        ledger=UsageLedger(),
        internet_runner=net,
        editor_runner=FakeWorkers(),
    )
    assert [(k["timeout_s"], k["max_turns"]) for k in net.kwargs] == [(450, 25), (450, 25)]
    assert (CFG.lookup_timeout_seconds, CFG.lookup_max_turns) == (300, 20)


def _timed_out(searches: int, fetches: int) -> Any:
    def answer(**kwargs: Any) -> None:
        stats = kwargs["stats_out"]
        stats.tools.update({"WebSearch": searches, "WebFetch": fetches})
        stats.timed_out = True
        stats.timeout_s = float(kwargs["timeout_s"])

    return answer


class _TimingOut(FakeInternet):
    """The internet fake, with a lookup that times out on the chosen step."""

    def __init__(self, step: str) -> None:
        super().__init__()
        self.step = step

    async def __call__(self, **kwargs: Any) -> dict[str, Any] | None:
        got = await super().__call__(**kwargs)
        is_challenge = kwargs["user_text"].startswith("You check an answer")
        if is_challenge == (self.step == "challenge"):
            _timed_out(6, 4)(**kwargs)
            return None
        return got


async def test_a_lookup_that_timed_out_says_so_in_the_hold() -> None:
    result = await _crew(_TimingOut("draft"), FakeWorkers())
    assert result.hold_reason == "lookup timed out after 300 s (6 searches, 4 fetches)"
    result = await _crew(_TimingOut("challenge"), FakeWorkers())
    assert result.hold_reason == (
        "the lookup of the challenger timed out after 300 s (6 searches, 4 fetches)"
    )


def test_a_lookup_with_no_answer_and_no_timeout_keeps_its_reason() -> None:
    assert crew.no_answer_reason("the researcher", WorkerStats()) == (
        "no answer from the researcher"
    )
    stats = WorkerStats(timed_out=True)
    assert crew.no_answer_reason("the researcher", stats).startswith("lookup timed out after ?")


async def test_a_source_the_lookup_never_returned_is_not_given_to_the_editor() -> None:
    net = FakeInternet(draft=lookup(PRIMARY, UNGIVEN), searched=[PRIMARY])
    workers = FakeWorkers()
    await _crew(net, workers)
    assert UNGIVEN not in workers.calls[0][1]


class _FetchingInternet(FakeInternet):
    """The internet fake, with a researcher that fetches `fetched` and names no source."""

    def __init__(self, fetched: list[str]) -> None:
        super().__init__(draft=lookup(), searched=fetched)
        self.fetched = fetched

    async def __call__(self, **kwargs: Any) -> dict[str, Any] | None:
        got = await super().__call__(**kwargs)
        if not kwargs["user_text"].startswith("You check an answer"):
            for url in self.fetched:
                for hook in (kwargs.get("hooks") or {}).get("PreToolUse", []):
                    await hook({"tool_name": "WebFetch", "tool_input": {"url": url}}, "t", None)
        return got


async def test_backfilled_sources_pass_the_independence_check() -> None:
    """The researcher named no source, but fetched two pages of two domains."""
    settings = research_settings(
        "internet:\n  fetch:\n    enabled: true\n    resolve_hosts: false\n"
    )
    net = _FetchingInternet([PRIMARY, SECONDARY])
    result = await crew.research_question(
        GROUNDED,
        GROUNDINGS,
        internet_settings=settings.internet,
        research_settings=CFG,
        editor_model=None,
        ledger=UsageLedger(),
        internet_runner=net,
        editor_runner=FakeWorkers(),
    )
    assert result.draft is not None
    assert [s.url for s in result.draft.sources] == [PRIMARY, SECONDARY]
    assert result.publish, result.hold_reason


def _keys(value: Any) -> set[str]:
    """Every key of a JSON value, at any depth."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_the_editor_schema_is_flat_and_has_no_limit() -> None:
    keys = _keys(crew.EDITOR_SCHEMA)
    assert not keys & {"$ref", "$defs", "maxLength", "maxItems", "used_for"}
    item = crew.EDITOR_SCHEMA["properties"]["sources"]["items"]
    assert set(item["properties"]) == {"url", "title", "trust"}
    enums = {name for name, prop in crew.EDITOR_SCHEMA["properties"].items() if "enum" in prop}
    assert enums == {"confidence", "verdict"}
    assert crew.EDITOR_SCHEMA["properties"]["evidence"]["items"] == {"type": "string"}


async def test_the_editor_gets_the_flat_schema() -> None:
    workers = FakeWorkers()
    schemas: list[Any] = []

    async def editor_runner(**kwargs: Any) -> Any:
        schemas.append(kwargs["schema"])
        return await workers(**kwargs)

    result = await crew.research_question(
        GROUNDED,
        GROUNDINGS,
        internet_settings=SETTINGS.internet,
        research_settings=CFG,
        editor_model=None,
        ledger=UsageLedger(),
        internet_runner=FakeInternet(),
        editor_runner=editor_runner,
    )
    assert result.publish
    assert schemas == [crew.EDITOR_SCHEMA]


def test_the_model_still_checks_the_limits_and_cuts_the_short_answer() -> None:
    pair = ((PRIMARY, "primary"), (SECONDARY, "secondary"))
    cut = _answer(*pair, answer_short="x" * 400)
    assert len(cut.answer_short) == crew.ANSWER_SHORT_CHARS and cut.answer_short.endswith("…")
    with pytest.raises(ValueError):
        _answer(*pair, evidence=["e"] * 31)
    with pytest.raises(ValueError):
        _answer(*pair, sources=[{"url": PRIMARY, "trust": "excellent"}])


def test_the_challenge_text_fits_the_lookup() -> None:
    text = challenge_text(GROUNDED, "x" * 9000)
    assert len(text) <= 2000
    assert text.startswith("You check an answer")


# -- the page --------------------------------------------------------------------


def _page(answer: EditorAnswer, run_id: int | None = 7) -> str:
    return render_page(
        question=GROUNDED,
        topic="houseplants/snake-plant.md",
        context=["houseplants/snake-plant.md", "journal/2026/10/15/x.md"],
        grounded_in=["gritty soil mix", "water to runoff"],
        answer=answer,
        day=date(2026, 10, 19),
        refresh_days=180,
        run_id=run_id,
    )


def test_a_page_has_the_frontmatter_and_the_body_in_order() -> None:
    text = _page(EditorAnswer.model_validate(GOOD_EDITOR))
    meta, body = frontmatter.split(text)
    assert meta["question"] == GROUNDED
    assert meta["topic"] == "houseplants/snake-plant.md"
    assert meta["context"] == ["houseplants/snake-plant.md", "journal/2026/10/15/x.md"]
    assert meta["researched_at"] == date(2026, 10, 19)
    assert meta["refresh_after"] == date(2027, 4, 17)
    assert meta["confidence"] == "high"
    assert meta["status"] == "published"
    assert meta["source"] == "research"
    assert meta["run_id"] == 7
    assert meta["answer_short"].startswith("About every 4 weeks")
    assert meta["sources"][0] == {
        "url": PRIMARY,
        "title": "Title 0",
        "trust": "primary",
        "retrieved": date(2026, 10, 19),
    }
    headings = [line for line in body.splitlines() if line.startswith("#")]
    assert headings == [
        f"# {GROUNDED}",
        "## Answer",
        "## Where this differs for us",
        "## Evidence",
        "## Disagreements",
        "## Sources",
    ]
    assert f"- [Title 0]({PRIMARY}), primary" in body


def test_links_not_in_the_sources_are_stripped() -> None:
    body = frontmatter.split(_page(EditorAnswer.model_validate(GOOD_EDITOR)))[1]
    assert f"[the guide]({PRIMARY})" in body
    assert "planted.example.com" not in body
    assert "a planted link" in body
    assert "[[" not in body and "secret-page" in body


def test_empty_parts_are_left_out() -> None:
    answer = _answer(
        (PRIMARY, "primary"), (SECONDARY, "secondary"), local_notes="", disagreements=""
    )
    meta, body = frontmatter.split(_page(answer, run_id=None))
    assert "## Where this differs for us" not in body
    assert "## Disagreements" not in body
    assert "run_id" not in meta


def test_a_page_over_64_kb_is_refused() -> None:
    answer = _answer((PRIMARY, "primary"), (SECONDARY, "secondary"))
    answer.answer = "x" * 70_000
    with pytest.raises(PageTooLong):
        _page(answer)


async def test_kept_lookups_go_to_the_editor_and_no_lookup_runs() -> None:
    from joshua_core.engine.internet import InternetAnswer

    net = FakeInternet()
    workers = FakeWorkers()
    draft = InternetAnswer.model_validate(lookup(PRIMARY, answer="Kept draft."))
    challenge = InternetAnswer.model_validate(lookup(SECONDARY, answer="Kept challenge."))
    result = await crew.research_question(
        GROUNDED,
        GROUNDINGS,
        internet_settings=SETTINGS.internet,
        research_settings=CFG,
        editor_model=None,
        ledger=UsageLedger(),
        internet_runner=net,
        editor_runner=workers,
        draft=draft,
        challenge=challenge,
    )
    assert net.texts == []
    assert "Kept draft." in workers.calls[0][1] and "Kept challenge." in workers.calls[0][1]
    assert result.publish
    assert (result.draft, result.challenge) == (draft, challenge)
