"""The sanitizer of worker answers: leaked markup is cut, a leaked field is
recovered, and a byte-exact field stays as it is.

The samples are the answers of research runs 8 and 9.
"""

from __future__ import annotations

from typing import Any

import pytest
from joshua_core.engine import internet
from joshua_core.engine.ephemeral import run_worker
from joshua_core.engine.sanitize import (
    CleanModel,
    clean_field,
    cleaning_context,
    log_cleaned,
    recover_fields,
    recovered_list,
)
from joshua_core.jobs.curator.workers import Claim, PatchPair, PlaceAnswer
from joshua_core.jobs.insights.worker import RaisedInsight, raise_insights
from joshua_core.jobs.research.crew import EditorAnswer
from joshua_core.jobs.research.raise_ import RaiseAnswer
from research_fakes import (
    CAVEAT,
    DISAGREE,
    PRIMARY,
    RUN8_DISAGREEMENTS,
    RUN9_ANSWER,
    SHORT,
    editor,
    run9_editor,
)

CLEAN_MARKDOWN = (
    "## Steps\n\n1. Water to runoff.\n2. Let it drain.\n\n"
    "- **Mix**: gritty, a < b by volume.\n- See `pot.md`.\n"
)


def _log_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("message") == "worker answer cleaned"
    ]


def _runner(result: Any):
    async def run(**kwargs: Any) -> Any:
        return result

    return run


# -- clean_field ----------------------------------------------------------------


def test_the_run_9_answer_is_cut_at_the_first_closing_tag() -> None:
    cleaned = clean_field(RUN9_ANSWER)
    assert cleaned.endswith(CAVEAT)
    assert "<" not in cleaned


def test_the_run_8_disagreements_are_cut_before_the_tool_fragment() -> None:
    assert clean_field(RUN8_DISAGREEMENTS).endswith("timeline at all.")


def test_clean_markdown_is_unchanged() -> None:
    assert clean_field(CLEAN_MARKDOWN) == CLEAN_MARKDOWN.strip()


def test_the_page_html_is_kept() -> None:
    text = "<details>\n<summary>More</summary>\nA line.<br>\n</details>"
    assert clean_field(text) == text


@pytest.mark.parametrize("text", ["a < b", "<3 the plant", "keep 2<3 and x <= y"])
def test_a_less_than_sign_is_not_a_tag(text: str) -> None:
    assert clean_field(text) == text


@pytest.mark.parametrize(
    ("text", "cleaned"),
    [
        ("<answer>Water it.", "Water it."),
        ("Water it.<parameter_name>evidence", "Water it."),
        ("Water it.<invoke name='x'>", "Water it."),
        ('Water it.<parameter name="evidence">', "Water it."),
        ("  plain  ", "plain"),
    ],
)
def test_other_leaks_are_removed(text: str, cleaned: str) -> None:
    assert clean_field(text) == cleaned


# -- recover_fields -------------------------------------------------------------


def test_the_leaked_fields_of_run_9_are_recovered() -> None:
    found = recover_fields(RUN9_ANSWER, ["answer_short", "confidence", "disagreements"])
    assert found == {"answer_short": SHORT, "confidence": "high", "disagreements": DISAGREE}


def test_a_field_with_no_block_is_not_recovered() -> None:
    assert recover_fields(RUN9_ANSWER, ["evidence", "local_notes"]) == {}


def test_a_parameter_block_is_recovered_and_an_open_block_stops_at_the_next_tag() -> None:
    text = (
        'x<parameter name="evidence">["One.", "Two."]</parameter>'
        "<local_notes>Ours dries fast. <details>kept</details><reason>r"
    )
    found = recover_fields(text, ["evidence", "local_notes"])
    assert recovered_list(found["evidence"]) == ["One.", "Two."]
    assert found["local_notes"] == "Ours dries fast. <details>kept</details>"


def test_a_recovered_list_reads_lines_and_bullets() -> None:
    assert recovered_list("- One.\n* Two.\n\nThree.") == ["One.", "Two.", "Three."]


# -- the models -----------------------------------------------------------------


def test_the_editor_answer_of_run_9_is_cleaned_and_recovered() -> None:
    answer = EditorAnswer.model_validate(run9_editor())
    assert answer.answer.endswith(CAVEAT)
    assert answer.disagreements == DISAGREE
    assert answer.evidence == []
    assert answer.local_notes == ""
    assert answer.answer_short == SHORT


def test_an_empty_short_answer_is_recovered_within_its_limit() -> None:
    answer = EditorAnswer.model_validate(run9_editor(answer_short=""))
    assert answer.answer_short == SHORT
    long = RUN9_ANSWER.replace(SHORT, "word " * 100)
    answer = EditorAnswer.model_validate(run9_editor(answer=long, answer_short=""))
    assert len(answer.answer_short) == 160
    assert answer.answer_short.endswith("…")


def test_a_field_the_model_filled_is_not_replaced() -> None:
    answer = EditorAnswer.model_validate(run9_editor(disagreements="Our own text."))
    assert answer.disagreements == "Our own text."


def test_recovered_evidence_is_a_list() -> None:
    raw = RUN9_ANSWER + "</disagreements>\n<evidence>\n- One.\n- Two.\n</evidence>"
    answer = EditorAnswer.model_validate(run9_editor(answer=raw))
    assert answer.evidence == ["One.", "Two."]


async def test_the_editor_answer_logs_the_cleaned_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO"):
        got = await run_worker(
            name="research-editor",
            system_prompt="s",
            user_text="u",
            answer=EditorAnswer,
            model=None,
            timeout_s=5,
            runner=_runner(run9_editor()),
        )
    assert got is not None
    assert _log_lines(caplog) == [
        {
            "message": "worker answer cleaned",
            "worker": "research-editor",
            "fields": ["answer", "disagreements"],
        }
    ]


async def test_a_clean_answer_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO"):
        got = await run_worker(
            name="research-editor",
            system_prompt="s",
            user_text="u",
            answer=EditorAnswer,
            model=None,
            timeout_s=5,
            runner=_runner(editor((PRIMARY, "primary"))),
        )
    assert got is not None
    assert _log_lines(caplog) == []


def test_the_internet_answer_of_run_8_is_cleaned() -> None:
    answer = internet.InternetAnswer.model_validate(
        {
            "answer": "<answer>Water to runoff.",
            "disagreements": RUN8_DISAGREEMENTS,
            "sources": [{"url": f"  {PRIMARY} ", "title": "Guide</title><x>"}],
        }
    )
    assert answer.disagreements.endswith("timeline at all.")
    assert answer.answer == "Water to runoff."
    assert answer.sources[0].url == PRIMARY
    assert answer.sources[0].title == "Guide"


def test_an_editor_source_url_is_only_trimmed() -> None:
    raw = editor((f" {PRIMARY}?a=<b_c> ", "primary"))
    answer = EditorAnswer.model_validate(raw)
    assert answer.sources[0].url == f"{PRIMARY}?a=<b_c>"


def test_a_patch_keeps_find_and_replace_byte_for_byte() -> None:
    pair = PatchPair.model_validate(
        {"find": " a </x> b ", "replace": "c </answer> d", "because": "It changed.</because>x"}
    )
    assert pair.find == " a </x> b "
    assert pair.replace == "c </answer> d"
    assert pair.because == "It changed."


def test_the_curator_answers_are_cleaned_but_place_names_are_not() -> None:
    place = PlaceAnswer.model_validate(
        {"section": "home</x>", "folder": "a/b", "confidence": "high", "reason": "Fits.</reason>"}
    )
    assert place.section == "home</x>"
    assert place.reason == "Fits."
    claim = Claim.model_validate({"a_says": "<a_says>One.", "b_says": "Two.</b_says>"})
    assert (claim.a_says, claim.b_says) == ("One.", "Two.")


def test_the_raise_answer_is_cleaned() -> None:
    answer = RaiseAnswer.model_validate(
        {
            "questions": [
                {
                    "question": "How often?</question><why>x",
                    "grounded_in": ["gritty mix</grounded_in>"],
                    "why": "It matters.",
                    "audience": "web</audience>",
                    "about_person": False,
                }
            ]
        }
    )
    q = answer.questions[0]
    assert (q.question, q.grounded_in, q.audience) == ("How often?", ["gritty mix"], "web")


def _insight(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "claim": "We water too often.",
        "why": "The page says four weeks.",
        "action": "Wait longer.",
        "severity": "act",
        "we_do": ["Water weekly."],
        "knowledge_says": ["Every four weeks."],
        "confidence": "high",
    }
    item.update(overrides)
    return item


def test_the_insight_answer_is_cleaned() -> None:
    got = RaisedInsight.model_validate(
        _insight(claim="We water too often.</claim><why>x", we_do=["Weekly.</we_do>"])
    )
    assert got.claim == "We water too often."
    assert got.we_do == ["Weekly."]


async def test_the_insight_worker_logs_once_for_its_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = {
        "insights": [
            _insight(claim="One.</claim>"),
            _insight(action="Two.</action>"),
            _insight(),
        ]
    }
    with caplog.at_level("INFO"):
        got = await raise_insights(user_text="u", model=None, runner=_runner(raw))
    assert got.candidates is not None and len(got.candidates) == 3
    assert _log_lines(caplog) == [
        {
            "message": "worker answer cleaned",
            "worker": "insights-insight",
            "fields": ["action", "claim"],
        }
    ]


def test_a_model_built_without_a_context_is_cleaned_and_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Note(CleanModel):
        text: str
        count: int = 0

    with caplog.at_level("INFO"):
        assert Note(text="Hi.</text>").text == "Hi."
        log_cleaned("note", cleaning_context())
    assert _log_lines(caplog) == []
