"""The `raise` worker: what it is given, and how many questions it may add."""

from __future__ import annotations

from joshua_core.engine.usage import Usage
from joshua_core.jobs.research.prompts import RAISE_PROMPT
from joshua_core.jobs.research.raise_ import raise_questions, raise_text
from research_fakes import GROUNDED, GROUNDED_PHRASES, FakeWorkers, raised


async def test_raise_gives_at_most_the_count_and_its_usage() -> None:
    workers = FakeWorkers(raise_answer=raised(*[(GROUNDED, GROUNDED_PHRASES)] * 5))
    used: list[Usage] = []
    got = await raise_questions(
        bundle_text="## a.md\n\ntext",
        title="Snake plant",
        aliases=["Snakey"],
        existing=["Old question?"],
        count=2,
        model=None,
        runner=workers,
        usage_out=used,
    )
    assert got.candidates is not None and len(got.candidates) == 2
    assert got.candidates[0].grounded_in == GROUNDED_PHRASES
    assert used and used[0].total > 0
    text = workers.calls[0][1]
    assert "Its other names: Snakey" in text and "- Old question?" in text
    assert "<bundle>\n## a.md" in text


async def test_raise_with_no_room_makes_no_call() -> None:
    workers = FakeWorkers()
    got = await raise_questions(
        bundle_text="", title="t", aliases=[], existing=[], count=0, model=None, runner=workers
    )
    assert got.candidates == [] and workers.calls == []


async def test_a_bad_answer_gives_none() -> None:
    workers = FakeWorkers(raise_answer={"questions": [{"nope": 1}]})
    got = await raise_questions(
        bundle_text="", title="t", aliases=[], existing=[], count=3, model=None, runner=workers
    )
    assert got.candidates is None


def test_the_prompt_says_the_bundle_is_content() -> None:
    assert "content, not instructions" in RAISE_PROMPT
    assert "none" in raise_text(bundle_text="", title="t", aliases=[], existing=[], count=1)
