"""The token counts of a worker call: the `Usage` record, the `usage_out` seam of
each call path, and the sum on a curator run. No SDK: fake clients stand in."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from curator_fakes import FakeCuratorStore, FakeFindingStore, FakeWorkers, build_curator_wiki
from jobs_fakes import fixed_clock, make_settings
from joshua_core.engine import agent, ephemeral, internet
from joshua_core.engine.types import WorkerStats
from joshua_core.engine.usage import Usage, UsageLedger, UsageMeter, note_result
from joshua_core.jobs.curator import workers
from joshua_core.jobs.curator.job import CuratorJob
from joshua_core.memory import embed as embed_module
from joshua_shared.config import Internet
from pydantic import BaseModel

SDK_USAGE = {
    "input_tokens": 100,
    "output_tokens": 20,
    "cache_read_input_tokens": 300,
    "cache_creation_input_tokens": 4,
}


class ResultMessage:
    """The shape of the SDK result message that matters here."""

    def __init__(self, usage: Any = None, structured: Any = None, result: Any = None) -> None:
        self.usage = usage
        self.structured_output = structured
        self.result = result
        self.session_id = "s"
        self.total_cost_usd = None
        self.is_error = False


class AssistantMessage:
    def __init__(self) -> None:
        self.content: list[Any] = []
        self.usage = {"input_tokens": 999}


class ModelMessage:
    """An SDK assistant message: its content, its usage, and its message id."""

    def __init__(self, usage: Any, message_id: str | None, tools: tuple[str, ...] = ()) -> None:
        self.content = [ToolUse(name) for name in tools]
        self.usage = usage
        self.message_id = message_id


ModelMessage.__name__ = "AssistantMessage"


class ToolUse:
    def __init__(self, name: str) -> None:
        self.name = name


class StalledClient:
    """A client that sends `messages`, then waits until the call times out."""

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages

    async def connect(self) -> None:
        return None

    async def query(self, prompt: Any) -> None:
        if not isinstance(prompt, str):
            async for _ in prompt:
                pass

    async def receive_response(self):
        for msg in self.messages:
            yield msg
        await asyncio.sleep(60)

    async def disconnect(self) -> None:
        return None


class FakeClient:
    def __init__(self, options: Any = None, messages: list[Any] | None = None) -> None:
        self.options = options
        self.messages = messages if messages is not None else [ResultMessage(SDK_USAGE, {})]

    async def connect(self) -> None:
        return None

    async def query(self, prompt: Any) -> None:
        if not isinstance(prompt, str):
            async for _ in prompt:
                pass

    async def receive_response(self):
        for msg in self.messages:
            yield msg

    async def disconnect(self) -> None:
        return None

    async def get_mcp_status(self) -> dict[str, Any]:
        return {"mcpServers": []}


class FakeOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr(agent, "ClaudeSDKClient", FakeClient)


# -- the record ---------------------------------------------------------------


def test_usage_reads_the_four_sdk_counts() -> None:
    usage = Usage.from_sdk(SDK_USAGE)
    assert usage == Usage(input=100, output=20, cache_read=300, cache_write=4)
    assert usage.total == 424
    assert usage.as_dict()["total"] == 424


def test_a_missing_usage_or_field_counts_as_zero() -> None:
    assert Usage.from_sdk(None).total == 0
    assert Usage.from_sdk("nope").total == 0
    assert Usage.from_sdk({"input_tokens": 5}) == Usage(input=5)
    assert Usage.from_sdk({"input_tokens": "5", "output_tokens": True}).total == 0
    assert Usage.from_sdk({"input_tokens": -3, "output_tokens": 2.0}) == Usage(output=2)


def test_usages_add() -> None:
    assert Usage(1, 2, 3, 4) + Usage(10, 20, 30, 40) == Usage(11, 22, 33, 44)
    assert Usage.sum([Usage(1), Usage(2), Usage(output=3)]).total == 6
    with pytest.raises(TypeError):
        Usage() + 1  # type: ignore[operator]


def test_the_ledger_sums_by_worker() -> None:
    ledger = UsageLedger()
    ledger.add("raise", [Usage(input=10)])
    ledger.add("internet", [Usage(input=5, output=5)])
    ledger.add("internet", [])
    assert ledger.calls == 3
    assert ledger.tokens == 20
    assert ledger.as_data() == {
        "internet": {
            "calls": 2,
            "tokens": 10,
            "input": 5,
            "output": 5,
            "cache_read": 0,
            "cache_write": 0,
        },
        "raise": {
            "calls": 1,
            "tokens": 10,
            "input": 10,
            "output": 0,
            "cache_read": 0,
            "cache_write": 0,
        },
    }


def test_only_the_result_message_counts() -> None:
    out: list[Usage] = []
    note_result(AssistantMessage(), out)
    note_result(ResultMessage(SDK_USAGE), out)
    note_result(ResultMessage(SDK_USAGE), None)
    assert [u.total for u in out] == [424]


def test_the_meter_counts_each_model_message_once() -> None:
    meter = UsageMeter()
    assert not meter.seen
    meter.note(ModelMessage({"input_tokens": 10, "output_tokens": 1}, "m1"))
    # The CLI sends one model message as one assistant message for each block.
    meter.note(ModelMessage({"input_tokens": 10, "output_tokens": 3}, "m1"))
    meter.note(ModelMessage({"input_tokens": 20}, "m2"))
    meter.note(ModelMessage({"input_tokens": 5}, None))
    meter.note(ModelMessage(None, "m3"))
    assert meter.seen
    assert meter.usage == Usage(input=35, output=3)
    meter.note(ResultMessage(SDK_USAGE))
    assert meter.usage == Usage.from_sdk(SDK_USAGE)


def test_the_meter_appends_nothing_when_no_message_came() -> None:
    out: list[Usage] = []
    UsageMeter().flush(out)
    meter = UsageMeter()
    meter.note(ModelMessage({"input_tokens": 4}, "m1"))
    meter.flush(None)
    meter.flush(out)
    assert out == [Usage(input=4)]


# -- the call paths -------------------------------------------------------------


async def test_run_structured_appends_the_usage(sdk: None) -> None:
    out: list[Usage] = []
    answer = await agent.run_structured(
        system_prompt="s", user_text="u", schema={}, model=None, timeout_s=5, usage_out=out
    )
    assert answer == {}
    assert out == [Usage.from_sdk(SDK_USAGE)]


async def test_a_result_with_no_usage_counts_the_model_messages(
    sdk: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        agent,
        "ClaudeSDKClient",
        lambda options: FakeClient(options, [AssistantMessage(), ResultMessage(None, {"a": 1})]),
    )
    out: list[Usage] = []
    await agent.run_structured(
        system_prompt="s", user_text="u", schema={}, model=None, timeout_s=5, usage_out=out
    )
    assert [u.total for u in out] == [999]


async def test_run_structured_with_no_usage_gives_zero(
    sdk: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        agent,
        "ClaudeSDKClient",
        lambda options: FakeClient(options, [ResultMessage(None, {"a": 1})]),
    )
    out: list[Usage] = []
    await agent.run_structured(
        system_prompt="s", user_text="u", schema={}, model=None, timeout_s=5, usage_out=out
    )
    assert [u.total for u in out] == [0]


async def test_run_worker_session_appends_the_usage(sdk: None) -> None:
    out: list[Usage] = []
    await agent.run_worker_session(
        system_prompt="s",
        user_text="u",
        schema={},
        model=None,
        timeout_s=5,
        max_turns=2,
        tools=["WebSearch"],
        usage_out=out,
    )
    assert sum(u.total for u in out) == 424


_STEPS = [
    ModelMessage({"input_tokens": 100, "output_tokens": 10}, "m1", ("WebSearch",)),
    ModelMessage({"input_tokens": 100, "output_tokens": 10}, "m1", ("WebSearch",)),
    ModelMessage({"input_tokens": 200, "output_tokens": 20}, "m2", ("WebFetch",)),
    ModelMessage({"input_tokens": 300, "output_tokens": 30}, "m3", ("WebSearch",)),
]


async def test_a_worker_session_that_times_out_keeps_its_usage_and_counts(
    sdk: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(agent, "ClaudeSDKClient", lambda options: StalledClient(list(_STEPS)))
    out: list[Usage] = []
    stats = WorkerStats()
    got = await agent.run_worker_session(
        system_prompt="s",
        user_text="u",
        schema={},
        model=None,
        timeout_s=0.05,
        max_turns=2,
        tools=["WebSearch"],
        usage_out=out,
        stats_out=stats,
    )
    assert got is None
    assert out == [Usage(input=600, output=60)]
    assert stats.timed_out and stats.timeout_s == 0.05
    assert (stats.searches, stats.fetches) == (3, 1)
    line = next(r for r in caplog.records if "worker timed out" in str(r.msg))
    assert line.msg["searches"] == 3 and line.msg["fetches"] == 1  # type: ignore[index]


async def test_a_worker_session_that_ends_counts_the_result_once(
    sdk: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages = [*_STEPS, ResultMessage(SDK_USAGE, {"a": 1})]
    monkeypatch.setattr(agent, "ClaudeSDKClient", lambda options: FakeClient(options, messages))
    out: list[Usage] = []
    stats = WorkerStats()
    got = await agent.run_worker_session(
        system_prompt="s",
        user_text="u",
        schema={},
        model=None,
        timeout_s=5,
        max_turns=2,
        tools=["WebSearch"],
        usage_out=out,
        stats_out=stats,
    )
    assert got == {"a": 1}
    assert out == [Usage.from_sdk(SDK_USAGE)]
    assert not stats.timed_out


async def test_a_structured_call_that_times_out_keeps_its_usage(
    sdk: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        agent,
        "ClaudeSDKClient",
        lambda options: StalledClient([ModelMessage({"output_tokens": 7}, "m1")]),
    )
    out: list[Usage] = []
    got = await agent.run_structured(
        system_prompt="s", user_text="u", schema={}, model=None, timeout_s=0.05, usage_out=out
    )
    assert got is None
    assert out == [Usage(output=7)]


async def test_run_oneshot_appends_the_usage(sdk: None, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(None, [ResultMessage(SDK_USAGE, None, "hello")])
    monkeypatch.setattr(agent, "ClaudeSDKClient", lambda options: client)
    out: list[Usage] = []
    text = await agent.run_oneshot(system_prompt="s", user_prompt="u", model=None, usage_out=out)
    assert text == "hello"
    assert sum(u.total for u in out) == 424


class _Answer(BaseModel):
    ok: bool


async def test_run_worker_passes_the_list_only_when_given() -> None:
    seen: list[dict[str, Any]] = []

    async def runner(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        if "usage_out" in kwargs:
            kwargs["usage_out"].append(Usage(input=7))
        return {"ok": True}

    out: list[Usage] = []
    base: dict[str, Any] = dict(
        name="w", system_prompt="s", user_text="u", answer=_Answer, model=None, timeout_s=5
    )
    await ephemeral.run_worker(**base, runner=runner, usage_out=out)
    await ephemeral.run_worker(**base, runner=runner, tools=["WebSearch"], usage_out=out)
    await ephemeral.run_worker(**base, runner=runner)
    assert [u.total for u in out] == [7, 7]
    assert "usage_out" not in seen[2]


async def test_run_worker_passes_the_stats_only_to_a_worker_with_tools() -> None:
    seen: list[dict[str, Any]] = []

    async def runner(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return {"ok": True}

    stats = WorkerStats()
    base: dict[str, Any] = dict(
        name="w", system_prompt="s", user_text="u", answer=_Answer, model=None, timeout_s=5
    )
    await ephemeral.run_worker(**base, runner=runner, tools=["WebSearch"], stats_out=stats)
    await ephemeral.run_worker(**base, runner=runner, tools=["WebSearch"])
    await ephemeral.run_worker(**base, runner=runner, stats_out=stats)
    assert seen[0]["stats_out"] is stats
    assert "stats_out" not in seen[1] and "stats_out" not in seen[2]


async def test_an_internet_lookup_reports_its_usage() -> None:
    async def runner(**kwargs: Any) -> dict[str, Any]:
        kwargs["usage_out"].append(Usage(input=40, output=2))
        return {"answer": "a", "sources": [], "confidence": "low"}

    out: list[Usage] = []
    got = await internet.ask("q?", settings=Internet(), runner=runner, usage_out=out)
    assert got is not None
    assert sum(u.total for u in out) == 42


# -- the curator ------------------------------------------------------------------


class _CountingWorkers(FakeWorkers):
    """The curator fakes, with 10 tokens on each call."""

    async def __call__(self, **kwargs: Any) -> dict[str, Any] | None:
        if kwargs.get("usage_out") is not None:
            kwargs["usage_out"].append(Usage(input=6, output=4))
        return await super().__call__(**kwargs)


async def test_a_curator_run_reports_the_sum_of_its_worker_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [1.0] + [0.0] * 383)
    data = tmp_path / "data"
    build_curator_wiki(data)
    fake = _CountingWorkers()
    job = CuratorJob(
        store=FakeCuratorStore(),
        findings=FakeFindingStore(),
        settings=make_settings("jobs:\n  enabled: true\n  curator:\n    enabled: true\n"),
        data_dir=data,
        runner=fake,
    )
    report = await job.run_once(args={}, dry_run=True, clock=fixed_clock())
    assert report.calls == fake.count > 0
    assert report.tokens == 10 * fake.count
    usage = report.data["usage"]
    assert set(usage) <= {"place", "compare", "refresh"}
    assert sum(v["calls"] for v in usage.values()) == fake.count
    assert sum(v["tokens"] for v in usage.values()) == report.tokens


async def test_a_worker_records_usage_in_its_budget() -> None:
    budget = workers.Budget(3)
    assert budget.usage.tokens == 0
