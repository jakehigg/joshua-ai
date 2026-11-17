"""The ``history`` tools of a worker: the two tools, and the scope of a read.

The SDK runner with a fake SDK client and a fake core. The scope itself is
applied in core (its ``history_api`` module); what these tests check
is what the container sends and what the spec allows.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from joshua_assignments import history, prompts
from joshua_assignments.session import HISTORY_TOOL_SPECS
from joshua_shared.contracts import HistoryMessage
from sdk_fakes import FakeCore, FakeSdk, done, history_call, init, result
from test_sdk_runner import Harness, spec

WS = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 3, 5, 12, 0, tzinfo=UTC)

SEARCH = "/v1/history/search"
READ = "/v1/history/read"


def searching(**args) -> list:
    """A script: the worker searches the history, then finishes."""
    return [init(), history_call("search_history", **args), *done("ok"), result("success")]


async def start_in_workspace(h: Harness, workspace: str, people=()) -> dict:
    started = await h.manager.start(
        "alex", spec(), workspace=workspace, workspace_people=list(people)
    )
    await h.runner.idle()
    return started


# --- the tools of the container ----------------------------------------------


async def test_an_assignment_calls_both_history_tools_in_its_container(tmp_path, channels) -> None:
    sdk = FakeSdk(
        [
            init(),
            history_call("search_history", query="receipt"),
            history_call("read_history", start="2026-03-04"),
            *done("ok"),
            result("success"),
        ]
    )
    core = FakeCore()
    h = Harness(tmp_path, sdk, core)
    started = await h.start()
    client = sdk.clients[0]

    # The server is in its own container, not on the gateway.
    assert "history" in client.options.mcp_servers
    assert set(client.options.mcp_servers["history"]) == {"type", "name", "instance"}
    assert {"mcp__history__search_history", "mcp__history__read_history"} <= set(
        client.options.allowed_tools
    )
    assert core.history_paths == [SEARCH, READ]
    search, read = core.history_bodies
    assert search == {"person": "alex", "workspace": None, "query": "receipt", "limit": 10}
    assert read["person"] == "alex" and read["cursor"] is None
    assert read["start"].startswith("2026-03-04T00:00:00")
    assert core.history_requests[0].headers["authorization"] == "Bearer assignments-fleet-token"
    # The worker reads the messages as they are, so it may quote one.
    first, second = (reply["content"][0]["text"] for reply in client.replies[:2])
    assert "the receipt of the market is in the drawer" in first
    assert "2 messages, newest first" in first
    assert "[End of this window.]" in second
    assert h.row(started["id"])["status"] == "done"


async def test_a_person_run_names_that_person_and_no_workspace(tmp_path, channels) -> None:
    core = FakeCore()
    h = Harness(tmp_path, FakeSdk(searching(query="receipt")), core)
    await h.start()
    assert core.history_bodies[0]["person"] == "alex"
    assert core.history_bodies[0]["workspace"] is None


async def test_a_shared_workspace_run_names_that_workspace(tmp_path, channels) -> None:
    core = FakeCore()
    h = Harness(tmp_path, FakeSdk(searching(query="receipt")), core)
    await start_in_workspace(h, WS, people=["alex", "mia"])
    assert core.history_bodies[0]["person"] == "alex"
    assert core.history_bodies[0]["workspace"] == WS


async def test_a_spec_that_leaves_history_out_has_no_history_tools(tmp_path, channels) -> None:
    sdk = FakeSdk([*done("ok"), result("success")])
    core = FakeCore()
    h = Harness(tmp_path, sdk, core)
    await h.start(tools=["gitlab"])
    options = sdk.clients[0].options
    assert "history" not in options.mcp_servers
    assert not [name for name in options.allowed_tools if name.startswith("mcp__history__")]
    assert core.history_bodies == []


async def test_a_spec_that_names_history_keeps_it(tmp_path, channels) -> None:
    sdk = FakeSdk(searching(query="receipt"))
    core = FakeCore()
    h = Harness(tmp_path, sdk, core)
    await h.start(tools=["history"])
    assert "history" in sdk.clients[0].options.mcp_servers
    assert core.history_paths == [SEARCH]


async def test_the_first_prompt_names_the_tools_only_when_the_session_holds_them(
    tmp_path, channels
) -> None:
    sdk = FakeSdk([*done("ok"), result("success")], [*done("ok"), result("success")])
    h = Harness(tmp_path, sdk, FakeCore())
    await h.start()
    await h.start(tools=["gitlab"])
    assert prompts.HISTORY in sdk.clients[0].prompt
    assert "search_history" not in sdk.clients[1].prompt


# --- the tools themselves -----------------------------------------------------


class FakeClient:
    """A core client that records its calls and answers with ``messages``."""

    def __init__(self, messages=None, cursor: str | None = None, fail: bool = False) -> None:
        self.messages = messages if messages is not None else []
        self.cursor = cursor
        self.fail = fail
        self.calls: list[tuple] = []

    async def search_history(self, person, workspace, query, limit, *, assignment_id=None):
        self.calls.append(("search", person, workspace, query, limit))
        if self.fail:
            return None
        return _Answer(self.messages)

    async def read_history(self, person, workspace, start, end, cursor, *, assignment_id=None):
        self.calls.append(("read", person, workspace, start, end, cursor))
        if self.fail:
            return None
        return _Answer(self.messages, self.cursor)


class _Answer:
    def __init__(self, messages, cursor: str | None = None) -> None:
        self.messages = messages
        self.cursor = cursor


def message(text: str = "hi", **fields) -> HistoryMessage:
    base = {"at": "2026-03-04T17:00:00+00:00", "direction": "in", "speaker": "Alex", "text": text}
    return HistoryMessage(**{**base, **fields})


def tools(core, workspace: str | None = None, tz: str = "America/New_York") -> history.HistoryTools:
    return history.HistoryTools(7, "alex", workspace, tz, core, now=lambda: NOW)


def text(reply: dict) -> str:
    return reply["content"][0]["text"]


async def test_a_search_gives_one_line_for_each_message() -> None:
    core = FakeClient([message("the receipt is in the drawer"), message("ok", direction="out")])
    reply = await tools(core).search_history({"query": "receipt"})
    lines = text(reply).splitlines()
    assert lines[0] == "2 messages, newest first. Times are in America/New_York."
    # The time of the message is in the timezone of the instance.
    assert lines[1] == "[2026-03-04 12:00] Alex: the receipt is in the drawer"
    assert lines[2] == "[2026-03-04 12:00] joshua: ok"
    assert core.calls == [("search", "alex", None, "receipt", 10)]


async def test_a_search_cuts_a_long_snippet_to_one_line() -> None:
    core = FakeClient([message("word " * 200)])
    line = text(await tools(core).search_history({"query": "word"})).splitlines()[1]
    assert line.endswith("...") and len(line) < 340


async def test_a_search_with_no_match_says_so() -> None:
    assert "No message" in text(await tools(FakeClient()).search_history({"query": "x"}))


async def test_a_search_with_no_query_asks_for_words() -> None:
    core = FakeClient()
    assert "Give words" in text(await tools(core).search_history({"query": "  "}))
    assert core.calls == []


@pytest.mark.parametrize(
    ("given", "sent"), [(None, 10), (0, 10), ("many", 10), (3, 3), (500, 50), (-2, 1)]
)
async def test_the_limit_of_a_search_is_cut_to_the_range(given, sent: int) -> None:
    core = FakeClient()
    await tools(core).search_history({"query": "x", "limit": given})
    assert core.calls[0][4] == sent


async def test_a_read_window_takes_local_times_and_pages() -> None:
    core = FakeClient([message("the whole message", cut=True)], cursor="c-2")
    reply = await tools(core).read_history({"start": "2026-03-04", "end": "2026-03-04T18:00"})
    lines = text(reply).splitlines()
    assert lines[0] == "Times are in America/New_York."
    assert lines[1] == "[2026-03-04 12:00] Alex: the whole message [message cut]"
    assert 'cursor "c-2"' in lines[2]
    _, person, workspace, start, end, cursor = core.calls[0]
    assert (person, workspace, cursor) == ("alex", None, None)
    assert start.isoformat() == "2026-03-04T00:00:00-05:00"
    assert end.isoformat() == "2026-03-04T18:00:00-05:00"


async def test_a_read_with_no_end_reads_up_to_now() -> None:
    core = FakeClient([message()])
    await tools(core).read_history({"start": "2026-03-04"})
    assert core.calls[0][4] == NOW


async def test_a_read_of_a_workspace_run_names_the_workspace() -> None:
    core = FakeClient([message()])
    await tools(core, workspace=WS).read_history({"start": "2026-03-04"})
    assert core.calls[0][2] == WS


async def test_a_read_with_a_cursor_sends_it() -> None:
    core = FakeClient([message()])
    await tools(core).read_history({"start": "2026-03-04", "cursor": "c-1"})
    assert core.calls[0][5] == "c-1"


@pytest.mark.parametrize("start", ["", "yesterday", "2026-13-40"])
async def test_a_time_that_is_not_iso_is_refused(start: str) -> None:
    core = FakeClient()
    assert "ISO times" in text(await tools(core).read_history({"start": start}))
    assert core.calls == []


async def test_an_end_before_the_start_is_refused() -> None:
    core = FakeClient()
    reply = await tools(core).read_history({"start": "2026-03-04", "end": "2026-03-03"})
    assert text(reply) == "end must be after start."
    assert core.calls == []


async def test_a_read_with_no_message_says_so() -> None:
    reply = await tools(FakeClient()).read_history({"start": "2026-03-04"})
    assert "No message" in text(reply)


async def test_a_history_core_cannot_read_says_so_and_does_not_fail() -> None:
    core = FakeClient(fail=True)
    assert "cannot be read" in text(await tools(core).search_history({"query": "x"}))
    assert "cannot be read" in text(await tools(core).read_history({"start": "2026-03-04"}))


async def test_a_stamp_that_is_not_a_time_stays_as_it_is() -> None:
    core = FakeClient([message(at="soon")])
    assert "[soon] Alex: hi" in text(await tools(core).search_history({"query": "x"}))


def test_the_server_has_the_two_read_tools_and_names_the_scope() -> None:
    assert set(HISTORY_TOOL_SPECS) == {"search_history", "read_history"}
    for description, _ in HISTORY_TOOL_SPECS.values():
        assert "shared workspace" in description or "same as search_history" in description


def test_the_handlers_are_the_tools_of_the_server() -> None:
    assert set(tools(FakeClient()).handlers()) == set(HISTORY_TOOL_SPECS)


async def test_a_read_in_utc_needs_no_timezone_of_its_own() -> None:
    core = FakeClient([message()])
    await tools(core, tz="UTC").read_history({"start": "2026-03-04T06:00+02:00"})
    assert core.calls[0][3] == datetime(2026, 3, 4, 4, 0, tzinfo=UTC)
