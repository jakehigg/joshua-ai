"""The SDK runner with a fake SDK client, a fake core, and a fake channels."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

from conftest import make_config, make_settings
from fakes import FIXED_NOW, FakeStore
from joshua_assignments.manager import Manager
from joshua_assignments.prompts import READ_PAGE_FIRST
from joshua_assignments.sdk_runner import (
    ASK_REPLY,
    AssignmentTools,
    SdkRunner,
    total_tokens,
    usage_ref,
)
from sdk_fakes import (
    HANG,
    SESSION,
    FakeCore,
    FakeSdk,
    assistant,
    call,
    init,
    rate_limit,
    result,
)

CONFIG = """
name: Home
timezone: UTC
people:
  - id: alex
    name: Alex
  - id: mia
    name: Mia
mcp:
  files:
    kind: builtin
    allow: all
  gitlab:
    type: http
    url: http://upstream.example/gitlab
    allow:
      - alex
  tasks:
    type: http
    url: http://upstream.example/tasks
    allow: all
  private:
    type: http
    url: http://upstream.example/private
    allow:
      - mia
  assignments:
    type: http
    url: http://assignments:8000/mcp
    allow:
      - alex
assignments:
  enabled: true
  slice_turns: 10
  limits:
    five_hour_pct: 70
    seven_day_pct: 85
    when_unknown: refuse
  people:
    alex:
      notify: telegram:dm:alex
"""

SECRET_ENV = {
    "PATH": "/usr/bin",
    "DATABASE_URL": "postgresql://joshua:pw@postgres/joshua",
    "ASSIGNMENTS_TOKENS": "alex=a-token",
    "ADDON_TOKEN": "addon",
    "JOSHUA_TOKEN_ASSIGNMENTS": "fleet",
}


def spec(**fields):
    return {"goal": "Audit the gitops repo\nand file one issue per finding.", **fields}


class Harness:
    def __init__(self, tmp_path: Path, sdk: FakeSdk, core: FakeCore, *, extra: str = "", **kw):
        self.cfg = make_config(text=CONFIG + extra)
        self.settings = make_settings()
        self.core = core
        self.sdk = sdk
        self.work = tmp_path / "work"
        self.slept: list[float] = []
        self.store = kw.pop("store", None) or FakeStore()
        start_check = kw.pop("start_check", True)

        async def sleep(seconds: float) -> None:
            self.slept.append(seconds)

        core_client = core.client(self.settings)
        self.manager = Manager(
            self.settings,
            self.store,
            load_config=lambda: self.cfg,
            clock=lambda: FIXED_NOW,
            core=core_client if start_check else None,
            runner=lambda m: SdkRunner(
                m,
                core=core_client,
                client_factory=sdk,
                work_root=self.work,
                environ=SECRET_ENV,
                sleep=sleep,
                watch_s=0.01,
                **kw,
            ),
        )

    @property
    def runner(self) -> SdkRunner:
        return self.manager.runner  # type: ignore[return-value]

    async def start(self, **fields) -> dict:
        started = await self.manager.start("alex", spec(**fields))
        await self.runner.idle()
        return started

    def row(self, assignment_id: int) -> dict:
        return self.store.rows[assignment_id]


def max_turns_slice(message: str, checkpoint: str | None = None) -> list:
    steps: list = [init()]
    if checkpoint:
        steps.append(call("checkpoint", note=checkpoint))
    steps += [assistant(message), result("error_max_turns")]
    return steps


async def test_three_slices_post_three_usage_rows_and_end_done(tmp_path, channels) -> None:
    sdk = FakeSdk(
        max_turns_slice("m1", "plan written. Next: step 1, read the repo"),
        max_turns_slice("m2", "step 1 done. Next: step 2, file the issues"),
        [
            assistant("m3"),
            call("finish", summary="Filed 2 issues.", follow_ups=["Check the CI", " "]),
            assistant("m4"),
            result("success", turns=4),
        ],
    )
    core = FakeCore()
    h = Harness(tmp_path, sdk, core)
    started = await h.start()
    row = h.row(started["id"])

    assert row["status"] == "done"
    assert row["summary"] == "Filed 2 issues."
    assert row["follow_ups"] == ["Check the CI"]
    assert row["session_id"] == SESSION
    # The last slice stops after finish, before its result: 2 model messages.
    assert row["turns"] == 22
    # Each slice of max turns has one model message, and the last has two.
    assert row["input"] == 400 and row["cache_read"] == 4000
    assert total_tokens(row) == 4 * 1125
    assert row["finished_at"] == FIXED_NOW

    assert [r["ref"] for r in core.usage_rows] == [usage_ref(started["id"], n) for n in (1, 2, 3)]
    first = core.usage_rows[0]
    assert first["kind"] == "assignment" and first["name"] == "assignment"
    assert first["worker"] == "sonnet" and first["run_id"] == started["id"]
    assert first["person_id"] == "alex" and first["model"] == "claude-sonnet-5"
    assert first["usage"] == {"input": 100, "output": 20, "cache_read": 1000, "cache_write": 5}
    assert core.usage_requests[0].headers["authorization"] == "Bearer assignments-fleet-token"
    # One read for the check of when_unknown at start, then one before each slice.
    assert core.limit_reads == 4

    [c1, c2, c3] = sdk.clients
    assert c1.options.resume is None
    assert c2.options.resume == SESSION and c3.options.resume == SESSION
    assert "Audit the gitops repo" in c1.prompt and row["page_path"] in c1.prompt
    assert c2.prompt.startswith("Continue. Budget: 5-hour window at 20% of your 70% ceiling")
    assert "weekly at 10% of 85%" in c2.prompt and "190 turns left" in c2.prompt
    assert "Last checkpoint: plan written. Next: step 1, read the repo" in c2.prompt
    assert READ_PAGE_FIRST not in c2.prompt
    assert c3.replies[0]["content"][0]["text"].startswith("The assignment is finished")
    assert c3.interrupted is True
    assert all(c.disconnected for c in sdk.clients)
    assert c1.options.max_turns == 10
    assert c1.options.effort == "medium" and c1.options.model == "claude-sonnet-5"

    assert [s["reason"] for s in h.store.slices] == ["turns", "turns", "finish"]
    assert h.store.slices[0]["limits_before"]["five_hour"]["used_pct"] == 20.0
    assert not (h.work / str(started["id"])).exists()
    [body] = channels.bodies
    assert "has ended: done" in body["text"] and "Filed 2 issues." in body["text"]
    status = await h.manager.status(await h.store.get(started["id"]))
    assert status["next_step"] == "step 2, file the issues"


async def test_over_the_ceiling_before_a_slice_parks_and_frees_the_slot(tmp_path, channels) -> None:
    core = FakeCore(five_hour=81.0)
    sdk = FakeSdk()
    h = Harness(tmp_path, sdk, core)
    started = await h.start(budget={"five_hour_pct": 80})
    row = h.row(started["id"])
    assert row["status"] == "paused_limits"
    assert sdk.clients == []
    assert (await h.store.counts()).get("running", 0) == 0
    assert row["stopped_at_limits"]["window"] == "five_hour"
    assert row["stopped_at_limits"]["used_pct"] == 81.0
    assert row["stopped_at_limits"]["ceiling"] == 80
    assert (h.work / str(started["id"])).is_dir()
    [body] = channels.bodies
    assert body["destination"] == "telegram:dm:alex"
    assert "has stopped and has a question" in body["text"]
    assert "the 5-hour window is at 81 percent, and my ceiling is 80 percent" in body["text"]
    assert "It resets at 14:05 on 2026-03-04." in body["text"]
    assert f"My plan and progress are at {row['page_path']}." in body["text"]
    assert "Tell me to continue, or stop me." in body["text"]


async def test_a_limits_stop_and_an_answer_resume_the_same_session(tmp_path, channels) -> None:
    core = FakeCore()
    sdk = FakeSdk(
        max_turns_slice("m1", "plan written. Next: step 1"),
        [call("finish", summary="Done."), result("success", turns=1)],
    )
    h = Harness(tmp_path, sdk, core)
    original_post = core.handler

    def go_over_after_the_first_slice(request):
        response = original_post(request)
        if request.method == "POST":
            core.five_hour = 81.0
        return response

    core.handler = go_over_after_the_first_slice  # type: ignore[method-assign]
    started = await h.start(budget={"five_hour_pct": 80})
    row = h.row(started["id"])
    assert row["status"] == "paused_limits"
    assert row["session_id"] == SESSION
    assert row["stopped_at_limits"]["next_step"] == "step 1"
    assert "My next step: step 1." in channels.bodies[0]["text"]

    core.five_hour = 20.0
    answered = await h.manager.answer(await h.store.get(started["id"]), "Go on.")
    assert answered["status"] == "running"
    await h.runner.idle()
    assert h.row(started["id"])["status"] == "done"
    resumed = sdk.clients[-1]
    assert resumed.options.resume == SESSION
    assert resumed.prompt.startswith(READ_PAGE_FIRST)
    assert "Answer: Go on." in resumed.prompt


async def test_a_rate_limit_event_over_the_ceiling_interrupts_and_parks(tmp_path, channels) -> None:
    core = FakeCore()
    sdk = FakeSdk([init(), assistant("m1"), rate_limit(0.85), assistant("m2"), result()])
    h = Harness(tmp_path, sdk, core)
    started = await h.start(budget={"five_hour_pct": 80})
    row = h.row(started["id"])
    [client] = sdk.clients
    assert client.interrupted is True and client.disconnected is True
    assert row["status"] == "paused_limits"
    record = row["stopped_at_limits"]
    assert (record["window"], record["used_pct"], record["source"]) == ("five_hour", 85.0, "event")
    assert record["windows"]["seven_day"]["used_pct"] == 10.0
    assert row["session_id"] == SESSION
    assert (h.work / str(started["id"])).is_dir()
    assert [r["usage"]["input"] for r in core.usage_rows] == [100]
    assert h.store.slices[0]["reason"] == "limits"
    assert "at 85 percent" in channels.bodies[0]["text"]


async def test_a_rate_limit_event_under_the_ceiling_does_not_stop(tmp_path, channels) -> None:
    sdk = FakeSdk([init(), rate_limit(0.5), call("finish", summary="ok"), result("success")])
    h = Harness(tmp_path, sdk, FakeCore())
    started = await h.start()
    assert h.row(started["id"])["status"] == "done"


async def test_when_unknown_refuse_refuses_a_spec_with_no_token_cap(tmp_path, channels) -> None:
    core = FakeCore()
    core.available = False
    h = Harness(tmp_path, FakeSdk(), core)
    refused = await h.manager.start("alex", spec())
    assert refused["status"] == "rejected" and refused["reason"] == "limits_unknown"
    assert h.store.rows == {}
    sdk = FakeSdk([call("finish", summary="ok"), result("success")])
    h = Harness(tmp_path, sdk, core)
    started = await h.start(budget={"tokens": 100000})
    assert h.row(started["id"])["status"] == "done"


async def test_when_unknown_run_runs_with_unknown_limits(tmp_path, channels) -> None:
    core = FakeCore()
    core.limit_status = 503
    sdk = FakeSdk([call("finish", summary="ok"), result("success")])
    h = Harness(tmp_path, sdk, core)
    h.cfg = make_config(text=CONFIG.replace("when_unknown: refuse", "when_unknown: run"))
    started = await h.start()
    assert h.row(started["id"])["status"] == "done"


async def test_unknown_limits_during_a_run_park_with_the_unknown_text(tmp_path, channels) -> None:
    core = FakeCore(seven_day=None)
    h = Harness(tmp_path, FakeSdk(), core, start_check=False)
    started = await h.start()
    row = h.row(started["id"])
    assert row["status"] == "paused_limits"
    assert row["stopped_at_limits"]["reason"] == "limits_unknown"
    assert row["stopped_at_limits"]["unknown"] == ["seven_day"]
    assert "cannot say how much of the subscription limits is used" in channels.bodies[0]["text"]


async def test_ask_parks_as_waiting_with_the_question(tmp_path, channels) -> None:
    sdk = FakeSdk([init(), call("ask", question="Which project?"), assistant(), result()])
    h = Harness(tmp_path, sdk, FakeCore())
    started = await h.start()
    row = h.row(started["id"])
    assert row["status"] == "waiting"
    assert row["open_question"] == "Which project?" and row["asked_at"] == FIXED_NOW
    assert sdk.clients[0].replies[0]["content"][0]["text"] == ASK_REPLY
    assert sdk.clients[0].interrupted is True
    assert (h.work / str(started["id"])).is_dir()
    [body] = channels.bodies
    assert "Which project?" in body["text"] and "answer_assignment" in body["text"]


async def test_a_session_error_fails_with_a_label_and_keeps_the_scratch(tmp_path, channels) -> None:
    sdk = FakeSdk([init(), assistant(), RuntimeError("boom at db.example")])
    h = Harness(tmp_path, sdk, FakeCore())
    started = await h.start()
    row = h.row(started["id"])
    assert row["status"] == "failed"
    assert row["error"] == "session_RuntimeError"
    assert "boom" not in json.dumps(row, default=str)
    assert (h.work / str(started["id"])).is_dir()
    assert [r["usage"]["input"] for r in h.core.usage_rows] == [100]
    assert "boom" not in channels.bodies[0]["text"]


async def test_a_failed_result_fails_with_its_status(tmp_path, channels) -> None:
    sdk = FakeSdk([result("error_during_execution", api_error_status=500)])
    h = Harness(tmp_path, sdk, FakeCore())
    started = await h.start()
    row = h.row(started["id"])
    assert (row["status"], row["error"]) == ("failed", "http_500")
    assert "host.example" not in json.dumps(row, default=str)


async def test_a_connect_error_fails(tmp_path, channels) -> None:
    sdk = FakeSdk()
    sdk.connect_error = OSError("no cli")
    h = Harness(tmp_path, sdk, FakeCore())
    started = await h.start()
    assert h.row(started["id"])["error"] == "session_OSError"


async def test_the_token_cap_ends_budget_exceeded(tmp_path, channels) -> None:
    sdk = FakeSdk(max_turns_slice("m1"), max_turns_slice("m2"))
    h = Harness(tmp_path, sdk, FakeCore())
    started = await h.start(budget={"tokens": 1000})
    row = h.row(started["id"])
    assert (row["status"], row["error"]) == ("budget_exceeded", "tokens")
    assert len(sdk.clients) == 1
    assert not (h.work / str(started["id"])).exists()


async def test_max_turns_ends_budget_exceeded_and_bounds_the_slice(tmp_path, channels) -> None:
    sdk = FakeSdk(max_turns_slice("m1"), max_turns_slice("m2"))
    h = Harness(tmp_path, sdk, FakeCore())
    started = await h.start(max_turns=15)
    row = h.row(started["id"])
    assert (row["status"], row["error"]) == ("budget_exceeded", "max_turns")
    assert [c.options.max_turns for c in sdk.clients] == [10, 5]


async def test_the_working_clock_interrupts_and_ends_time_exceeded(tmp_path, channels) -> None:
    ticks = iter(range(0, 10**6, 100))
    sdk = FakeSdk([init(), assistant(), HANG])
    h = Harness(tmp_path, sdk, FakeCore(), monotonic=lambda: float(next(ticks)))
    started = await h.start(working_time="5m")
    row = h.row(started["id"])
    assert (row["status"], row["error"]) == ("time_exceeded", "working_time")
    assert sdk.clients[0].interrupted is True
    assert row["working_s"] >= 300
    assert h.store.slices[0]["reason"] == "time"


async def test_no_working_time_left_ends_before_a_slice(tmp_path, channels) -> None:
    store = FakeStore()
    h = Harness(tmp_path, FakeSdk(), FakeCore(), store=store)
    started = await h.manager.start("alex", spec(working_time="1m"))
    await h.runner.cancel({"id": started["id"]})
    store.rows[started["id"]].update(status="queued", working_s=60, started_at=FIXED_NOW)
    await h.manager.dispatch()
    await h.runner.idle()
    assert h.row(started["id"])["status"] == "time_exceeded"


async def test_cancel_stops_the_session_records_the_slice_and_removes_the_scratch(
    tmp_path, channels
) -> None:
    sdk = FakeSdk([init(), assistant(), HANG])
    h = Harness(tmp_path, sdk, FakeCore())
    started = await h.manager.start("alex", spec())
    for _ in range(50):
        if sdk.clients and sdk.clients[0].prompt:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    result_ = await h.manager.cancel(await h.store.get(started["id"]))
    assert result_["status"] == "cancelled"
    assert h.row(started["id"])["status"] == "cancelled"
    assert sdk.clients[0].disconnected is True
    assert h.store.slices[0]["reason"] == "cancelled"
    assert h.row(started["id"])["input"] == 100
    assert not (h.work / str(started["id"])).exists()
    assert channels.bodies == []


async def test_a_restart_resumes_the_stored_session_with_the_page_first(tmp_path, channels) -> None:
    store = FakeStore()
    h = Harness(tmp_path, FakeSdk(), FakeCore(), store=store)
    row = await store.create(
        person_id="alex",
        conversation_id=None,
        spec={
            **spec(),
            **{"model": "sonnet", "budget": {"five_hour_pct": 70, "seven_day_pct": 85}},
        },
        model="claude-sonnet-5",
        page_path=lambda i: f"assignments/{i}-audit.md",
    )
    await store.update(row["id"], status="running", started_at=FIXED_NOW, session_id="sess-9")
    await store.add_checkpoint(row["id"], "step 1 done. Next: step 2")
    h.sdk.scripts.append([call("finish", summary="ok"), result("success")])
    await h.manager.open()
    await h.runner.idle()
    [client] = h.sdk.clients
    assert client.options.resume == "sess-9"
    assert client.prompt.startswith(READ_PAGE_FIRST)
    assert "Last checkpoint: step 1 done. Next: step 2" in client.prompt
    assert h.row(row["id"])["status"] == "done"


async def test_the_session_holds_the_persons_gateway_servers_only(tmp_path, channels) -> None:
    sdk = FakeSdk([call("finish", summary="ok"), result("success")])
    h = Harness(tmp_path, sdk, FakeCore())
    started = await h.start()
    options = sdk.clients[0].options
    assert set(options.mcp_servers) == {"files", "gitlab", "tasks", "assignment"}
    gitlab = options.mcp_servers["gitlab"]
    assert gitlab["url"] == "http://gateway:8000/gitlab"
    assert gitlab["headers"]["X-Joshua-Conversation"] == f"assignment:{started['id']}"
    assert gitlab["headers"]["X-Joshua-Role"] == "member"
    assert gitlab["headers"]["X-Joshua-Person"] == "alex"
    env = options.env
    for name in ("DATABASE_URL", "ASSIGNMENTS_TOKENS", "ADDON_TOKEN", "JOSHUA_TOKEN_ASSIGNMENTS"):
        assert env[name] == ""
    assert "PATH" not in env
    assert env["ANTHROPIC_BASE_URL"] == "http://core:8000/v1/model"
    assert env["HOME"] == str(h.work / str(started["id"]) / ".home")
    assert env["CLAUDE_CONFIG_DIR"].startswith(env["HOME"])
    assert options.cwd == str(h.work / str(started["id"]))


async def test_spec_tools_narrow_the_servers_and_keep_files(tmp_path, channels) -> None:
    sdk = FakeSdk([call("finish", summary="ok"), result("success")])
    h = Harness(tmp_path, sdk, FakeCore())
    await h.start(tools=["gitlab", "private", "assignments"])
    assert set(sdk.clients[0].options.mcp_servers) == {"files", "gitlab", "assignment"}


async def test_the_wait_and_checkpoint_tools(tmp_path, channels) -> None:
    sdk = FakeSdk(
        [
            call("wait", seconds=5000),
            call("wait", seconds="soon"),
            call("checkpoint", note="  "),
            call("ask", question=""),
            call("finish", summary=""),
            call("finish", summary="ok", follow_ups="one"),
            result("success"),
        ]
    )
    h = Harness(tmp_path, sdk, FakeCore())
    started = await h.start()
    replies = [r["content"][0]["text"] for r in sdk.clients[0].replies]
    assert h.slept == [600, 1]
    assert replies[:2] == ["Waited 600 seconds.", "Waited 1 seconds."]
    assert [r.get("is_error") for r in sdk.clients[0].replies[2:5]] == [True, True, True]
    assert h.row(started["id"])["follow_ups"] == ["one"]


async def test_a_runner_without_core_fails_not_configured(tmp_path, channels) -> None:
    h = Harness(tmp_path, FakeSdk(), FakeCore(), start_check=False)
    h.manager.settings = make_settings(core_url=None)
    started = await h.start()
    assert h.row(started["id"])["error"] == "not_configured"


async def test_a_failed_usage_post_is_tried_again(tmp_path, channels) -> None:
    core = FakeCore()
    core.usage_replies = [503, 200]
    sdk = FakeSdk([assistant(), call("finish", summary="ok"), result("success")])
    h = Harness(tmp_path, sdk, core)
    await h.start()
    assert len(core.usage_requests) == 2 and len(core.usage_rows) == 1


async def test_sweep_removes_ended_scratch_and_keeps_recent_failures(tmp_path, channels) -> None:
    h = Harness(tmp_path, FakeSdk(), FakeCore())
    store = h.store
    ids = []
    for status in ("failed", "failed", "done", "waiting"):
        row = await store.create(
            person_id="alex",
            conversation_id=None,
            spec=spec(),
            model="m",
            page_path=lambda i: f"assignments/{i}.md",
        )
        ids.append(row["id"])
        await store.update(row["id"], status=status, finished_at=FIXED_NOW)
    store.rows[ids[0]]["finished_at"] = FIXED_NOW - timedelta(days=8)
    for i in [*ids, 99]:
        (h.work / str(i)).mkdir(parents=True)
    (h.work / "notes").mkdir()
    removed = await h.runner.sweep()
    assert removed == sorted([ids[0], ids[2], 99])
    assert (h.work / str(ids[1])).is_dir() and (h.work / str(ids[3])).is_dir()
    assert (h.work / "notes").is_dir()


async def test_sweep_with_no_work_root_does_nothing(tmp_path, channels) -> None:
    h = Harness(tmp_path, FakeSdk(), FakeCore())
    assert await h.runner.sweep() == []


async def test_an_unexpected_runner_error_fails_with_a_label(tmp_path, channels) -> None:
    h = Harness(tmp_path, FakeSdk(), FakeCore())

    async def broken(*args, **kwargs):
        raise ValueError("secret")

    h.store.next_slice_n = broken  # type: ignore[method-assign]
    started = await h.start()
    assert h.row(started["id"])["error"] == "runner_ValueError"


async def test_a_run_of_a_row_that_is_not_running_does_nothing(tmp_path, channels) -> None:
    h = Harness(tmp_path, FakeSdk(), FakeCore())
    await h.runner.start({"id": 42})
    await h.runner.idle()
    assert h.sdk.clients == []


async def test_assignment_tools_stop_only_after_ask_or_finish() -> None:
    class Host:
        store = FakeStore()
        clock = staticmethod(lambda: FIXED_NOW)

    tools = AssignmentTools(1, Host())  # type: ignore[arg-type]
    assert tools.stop_requested is False
    await tools.checkpoint({"note": "x"})
    assert tools.stop_requested is False
    await tools.finish({"summary": "done"})
    assert tools.stop_requested is True
