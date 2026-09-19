"""The ephemeral worker: no tools, no history, a fixed answer, a safe failure.

The runner is a fake here, so no SDK and no network are needed. The tests that
prove "no tools" and "no history" read the options the real SDK path builds.
"""

from __future__ import annotations

import asyncio
from typing import Any

from joshua_core.engine.ephemeral import run_worker
from pydantic import BaseModel


class Answer(BaseModel):
    kind: str
    count: int = 0


def _runner(result: Any):
    calls: list[dict[str, Any]] = []

    async def run(**kwargs: Any) -> Any:
        calls.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    return run, calls


async def test_a_good_answer_is_returned_as_the_model() -> None:
    run, calls = _runner({"kind": "receipt", "count": 2})

    answer = await run_worker(
        name="w",
        system_prompt="s",
        user_text="u",
        answer=Answer,
        model="m",
        timeout_s=5,
        runner=run,
    )

    assert answer == Answer(kind="receipt", count=2)
    assert calls[0]["schema"]["properties"]["kind"]["type"] == "string"


async def test_an_answer_that_does_not_match_the_shape_is_refused() -> None:
    run, _ = _runner({"kind": "receipt", "count": "not a number"})

    answer = await run_worker(
        name="w",
        system_prompt="s",
        user_text="u",
        answer=Answer,
        model="m",
        timeout_s=5,
        runner=run,
    )

    assert answer is None


async def test_no_answer_gives_none() -> None:
    run, _ = _runner(None)

    answer = await run_worker(
        name="w",
        system_prompt="s",
        user_text="u",
        answer=Answer,
        model="m",
        timeout_s=5,
        runner=run,
    )

    assert answer is None


async def test_a_worker_that_raises_never_raises_into_the_caller() -> None:
    run, _ = _runner(RuntimeError("the model is down"))

    answer = await run_worker(
        name="w",
        system_prompt="s",
        user_text="u",
        answer=Answer,
        model="m",
        timeout_s=5,
        runner=run,
    )

    assert answer is None


async def test_a_worker_that_hangs_is_given_up_on() -> None:
    """The deadline belongs to the runner; a caller waits for nothing longer."""

    async def run(**kwargs: Any) -> Any:
        await asyncio.sleep(kwargs["timeout_s"])
        return None

    answer = await asyncio.wait_for(
        run_worker(
            name="w",
            system_prompt="s",
            user_text="u",
            answer=Answer,
            model="m",
            timeout_s=0.01,
            runner=run,
        ),
        timeout=5,
    )

    assert answer is None


async def test_the_file_and_the_objective_reach_the_runner() -> None:
    run, calls = _runner({"kind": "photo"})

    await run_worker(
        name="w",
        system_prompt="system",
        user_text="objective",
        answer=Answer,
        model="claude-haiku-4-5",
        timeout_s=9,
        image=(b"bytes", "image/png"),
        runner=run,
    )

    call = calls[0]
    assert call["system_prompt"] == "system"
    assert call["user_text"] == "objective"
    assert call["image"] == (b"bytes", "image/png")
    assert call["model"] == "claude-haiku-4-5"
    assert call["timeout_s"] == 9


async def test_the_worker_sees_no_message_of_the_conversation() -> None:
    """Nothing but the objective and the file reaches the model."""
    run, calls = _runner({"kind": "photo"})

    await run_worker(
        name="w",
        system_prompt="system",
        user_text="Describe this file.",
        answer=Answer,
        model="m",
        timeout_s=5,
        image=(b"bytes", "image/png"),
        runner=run,
    )

    sent = " ".join(str(v) for v in calls[0].values())
    assert "conversation" not in sent.lower()
    assert set(calls[0]) == {
        "system_prompt",
        "user_text",
        "schema",
        "model",
        "timeout_s",
        "image",
        "document",
    }
