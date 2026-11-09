"""The question loop, the resume after a restart, and extend_assignment, with the SDK runner.

A fake SDK client, a fake core, and a fake channels. The last test clones a
local bare repository from inside a session, through the git proxy, and
checks that the token never reaches the session.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fakes import FIXED_NOW, FakeStore
from joshua_assignments import prompts
from joshua_assignments.git_proxy import GitProxy
from sdk_fakes import SESSION, FakeCore, FakeSdk, assistant, call, done, init, result, shell
from test_git_proxy import TOKEN, FakeGitHost, basic, make_bare
from test_sdk_runner import SECRET_ENV, Harness, max_turns_slice, spec

GIT_EXTRA = """
  repos:
    - gitlab.example.net/infra/*
  platforms:
    gitlab.example.net:
      kind: gitlab
      token_env: ASSIGNMENTS_GITLAB_TOKEN
"""


def asking(question: str = "Which project?") -> list:
    return [init(), call("ask", question=question), assistant(), result()]


async def test_ask_frees_the_slot_and_the_answer_resumes_the_same_session(
    tmp_path, channels
) -> None:
    sdk = FakeSdk(
        asking(),
        [*done("Second done."), result("success")],
        [*done("First done."), result("success")],
    )
    h = Harness(tmp_path, sdk, FakeCore())
    first = await h.start()
    assert h.row(first["id"])["status"] == "waiting"
    # max_concurrent is 1, and the waiting assignment holds no slot.
    second = await h.start()
    assert h.row(second["id"])["status"] == "done"
    question = channels.bodies[0]
    assert question["destination"] == "telegram:dm:alex"
    assert "has stopped and has a question" in question["text"]
    assert "Which project?" in question["text"]

    answered = await h.manager.answer(await h.store.get(first["id"]), "The ops project.")
    assert answered["status"] == "running"
    await h.runner.idle()
    resumed = sdk.clients[-1]
    assert resumed.options.resume == SESSION
    assert resumed.prompt.startswith(
        "Read your plan with `read_plan` before anything else.\nAnswer: The ops project."
    )
    row = h.row(first["id"])
    assert row["status"] == "done"
    # The answer went to the model once, so a restart cannot send it again.
    assert row["answer"] is None and row["open_question"] is None


async def test_an_answer_over_the_ceiling_stays_parked_and_says_so(tmp_path, channels) -> None:
    core = FakeCore()
    sdk = FakeSdk(asking(), [*done("ok"), result("success")])
    h = Harness(tmp_path, sdk, core)
    started = await h.start(budget={"five_hour_pct": 80})
    core.five_hour = 85.0
    reply = await h.manager.answer(await h.store.get(started["id"]), "Use ops.")
    assert reply["status"] == "waiting"
    assert "stays waiting" in reply["message"]
    assert "5-hour window is at 85 percent, and its ceiling is 80 percent" in reply["message"]
    assert "It resets at 14:05 on 2026-03-04." in reply["message"]
    row = h.row(started["id"])
    assert row["status"] == "waiting" and row["answer"] == "Use ops."
    assert len(sdk.clients) == 1

    # Later, under the ceiling: the stored answer goes first, then the new text.
    core.five_hour = 10.0
    reply = await h.manager.answer(await h.store.get(started["id"]), "continue")
    assert reply["status"] == "running"
    await h.runner.idle()
    assert "Answer: Use ops.\ncontinue" in sdk.clients[-1].prompt
    assert h.row(started["id"])["status"] == "done"


async def test_an_answer_with_unknown_limits_and_no_token_cap_stays_parked(
    tmp_path, channels
) -> None:
    core = FakeCore()
    h = Harness(tmp_path, FakeSdk(asking()), core)
    started = await h.start()
    core.available = False
    reply = await h.manager.answer(await h.store.get(started["id"]), "continue")
    assert reply["status"] == "waiting"
    assert "cannot say how much" in reply["message"]


async def test_a_waiting_assignment_waits_forever_and_a_restart_leaves_it(
    tmp_path, channels
) -> None:
    store = FakeStore()
    h = Harness(tmp_path, FakeSdk(asking()), FakeCore(), store=store)
    started = await h.start()
    assert h.row(started["id"])["status"] == "waiting"
    # A year later, a new container starts: the sweep and the queue leave it.
    later = Harness(tmp_path, FakeSdk(), FakeCore(), store=store)
    later.manager.clock = lambda: FIXED_NOW + timedelta(days=365)
    await later.manager.open()
    await later.runner.idle()
    assert later.sdk.clients == []
    row = store.rows[started["id"]]
    assert row["status"] == "waiting" and row["open_question"] == "Which project?"
    assert (later.work / str(started["id"])).is_dir()


async def make_running(store: FakeStore, work: Path, *, session: str | None, **fields) -> int:
    row = await store.create(
        person_id="alex",
        conversation_id=None,
        spec={**spec(), "model": "sonnet", "budget": {"five_hour_pct": 70, "seven_day_pct": 85}},
        model="claude-sonnet-5",
    )
    await store.update(
        row["id"], status="running", started_at=FIXED_NOW, session_id=session, **fields
    )
    if session:
        (work / str(row["id"]) / ".home").mkdir(parents=True)
    return row["id"]


async def test_a_restart_resumes_a_running_row_with_the_restart_line_and_no_old_answer(
    tmp_path, channels
) -> None:
    store = FakeStore()
    sdk = FakeSdk([*done("ok"), result("success")])
    h = Harness(tmp_path, sdk, FakeCore(), store=store)
    running = await make_running(store, h.work, session="sess-7")
    waiting = await make_running(store, h.work, session="sess-8")
    await store.update(waiting, status="waiting", open_question="Q?")
    await h.manager.open()
    await h.runner.idle()
    [client] = sdk.clients
    assert client.options.resume == "sess-7"
    assert client.prompt.startswith(prompts.RESTARTED)
    assert "Answer:" not in client.prompt
    assert store.rows[running]["status"] == "done"
    assert store.rows[waiting]["status"] == "waiting"


async def test_an_answer_stored_before_a_restart_is_sent_once(tmp_path, channels) -> None:
    store = FakeStore()
    sdk = FakeSdk([*done("ok"), result("success")])
    h = Harness(tmp_path, sdk, FakeCore(), store=store)
    running = await make_running(store, h.work, session="sess-7", answer="Use ops.")
    await h.manager.open()
    await h.runner.idle()
    [client] = sdk.clients
    assert client.prompt.startswith(prompts.READ_PLAN_FIRST + "\nAnswer: Use ops.")
    assert store.rows[running]["answer"] is None


async def test_a_running_row_with_no_session_starts_fresh(tmp_path, channels) -> None:
    store = FakeStore()
    sdk = FakeSdk([*done("ok"), result("success")])
    h = Harness(tmp_path, sdk, FakeCore(), store=store)
    row_id = await make_running(store, h.work, session=None)
    await h.manager.open()
    await h.runner.idle()
    [client] = sdk.clients
    assert client.options.resume is None
    assert client.prompt.startswith(f"Assignment {row_id}: Audit the gitops repo\n")


async def test_a_lost_scratch_starts_a_new_session_that_reads_the_plan(tmp_path, channels) -> None:
    store = FakeStore()
    sdk = FakeSdk([*done("ok"), result("success")])
    h = Harness(tmp_path, sdk, FakeCore(), store=store)
    row_id = await make_running(store, h.work, session="sess-7")
    shutil.rmtree(h.work / str(row_id))
    await h.manager.open()
    await h.runner.idle()
    [client] = sdk.clients
    assert client.options.resume is None
    assert client.prompt.startswith(prompts.SCRATCH_LOST)
    assert store.rows[row_id]["status"] == "done"


async def test_extend_resumes_a_budget_exceeded_assignment(tmp_path, channels) -> None:
    sdk = FakeSdk(max_turns_slice("m1"), [*done("ok"), result("success")])
    h = Harness(tmp_path, sdk, FakeCore())
    started = await h.start(budget={"tokens": 1000})
    assert h.row(started["id"])["status"] == "budget_exceeded"
    reply = await h.manager.extend(await h.store.get(started["id"]), tokens=100000)
    assert reply["status"] == "running" and "continues its session" in reply["message"]
    await h.runner.idle()
    resumed = sdk.clients[-1]
    assert resumed.options.resume == SESSION
    assert resumed.prompt.startswith(prompts.EXTENDED)
    row = h.row(started["id"])
    assert row["status"] == "done" and row["error"] is None


async def test_extend_resumes_a_time_exceeded_assignment(tmp_path, channels) -> None:
    store = FakeStore()
    sdk = FakeSdk([*done("ok"), result("success")])
    h = Harness(tmp_path, sdk, FakeCore(), store=store)
    row_id = await make_running(store, h.work, session="sess-7", working_s=3600)
    await store.update(row_id, status="time_exceeded", error="working_time", finished_at=FIXED_NOW)
    store.rows[row_id]["spec"]["working_time"] = "1h"
    reply = await h.manager.extend(await store.get(row_id), working_time="2h")
    assert reply["status"] == "running"
    await h.runner.idle()
    assert sdk.clients[-1].options.resume == "sess-7"
    assert store.rows[row_id]["status"] == "done"
    assert store.rows[row_id]["finished_at"] == FIXED_NOW


async def test_extend_raises_max_turns_and_refuses_an_ended_assignment(tmp_path, channels) -> None:
    store = FakeStore()
    h = Harness(tmp_path, FakeSdk(), FakeCore(), store=store)
    row_id = await make_running(store, h.work, session="sess-7")
    await store.update(row_id, status="waiting")
    reply = await h.manager.extend(await store.get(row_id), max_turns=500)
    assert reply["max_turns"] == 500 and reply["status"] == "waiting"
    assert (await h.manager.extend(await store.get(row_id), max_turns=500))["reason"] == (
        "not_raised"
    )
    assert (await h.manager.extend(await store.get(row_id), max_turns=10**6))["reason"] == (
        "not_raised"
    )
    for status in ("done", "failed", "cancelled"):
        await store.update(row_id, status=status)
        reply = await h.manager.extend(await store.get(row_id), tokens=10**6)
        assert reply["reason"] == "finished"


@pytest.mark.parametrize(
    ("fields", "has_tasks"),
    [({"tasks_project": "ops"}, True), ({"tasks_project": "ops", "tools": ["gitlab"]}, False)],
)
async def test_the_follow_up_rule_follows_the_servers_of_the_session(
    tmp_path, channels, fields, has_tasks
) -> None:
    sdk = FakeSdk([*done("ok"), result("success")])
    h = Harness(tmp_path, sdk, FakeCore())
    await h.start(**fields)
    [client] = sdk.clients
    assert ("tasks" in client.options.mcp_servers) is has_tasks
    assert ("tasks project ops" in client.prompt) is has_tasks
    assert "page" not in client.prompt


@pytest.mark.skipif(shutil.which("git") is None, reason="git binary required")
async def test_a_session_clones_through_the_proxy_and_never_holds_the_token(
    tmp_path, channels, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    bare = make_bare(tmp_path)
    host = FakeGitHost(bare, basic("oauth2", TOKEN))
    environ = {**SECRET_ENV, "ASSIGNMENTS_GITLAB_TOKEN": TOKEN}
    store = FakeStore()
    from conftest import make_config
    from test_sdk_runner import CONFIG

    git = GitProxy(
        store,
        lambda: make_config(text=CONFIG + GIT_EXTRA),
        environ=environ,
        transport=httpx.MockTransport(host.handler),
    )
    seen: dict = {}

    async def run_git(options, *args: str, cwd: str | None = None) -> int:
        # The CLI starts with the environment of the manager plus options.env,
        # and Bash inherits it.
        env = {**environ, "PATH": os.environ["PATH"], **options.env}
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd or options.cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        seen.setdefault("out", []).append(out.decode())
        seen["env"] = env
        return proc.returncode or 0

    async def clone(options) -> dict:
        seen["clone"] = await run_git(
            options, "clone", "https://gitlab.example.net/infra/gitops", "repos/gitops"
        )
        return {}

    async def push(options) -> dict:
        repo = str(Path(options.cwd) / "repos" / "gitops")
        seen["count"] = await run_git(options, "rev-list", "--count", "HEAD", cwd=repo)
        seen["shallow"] = seen["out"][-1].strip()
        # What a command of the session finds in its scratch and its HOME.
        seen["files"] = await asyncio.to_thread(read_tree, Path(options.cwd))
        seen["gitconfig"] = seen["files"][Path(options.env["HOME"]) / ".gitconfig"].decode()
        ident = ("-c", "user.name=T", "-c", "user.email=t@example.net")
        await run_git(options, *ident, "commit", "--allow-empty", "-qm", "x", cwd=repo)
        seen["push"] = await run_git(options, "push", "origin", "HEAD:main", cwd=repo)
        return {}

    sdk = FakeSdk([init(), shell(clone), shell(push), *done("ok"), result()])
    h = Harness(tmp_path, sdk, FakeCore(), store=store, extra=GIT_EXTRA, git=git)
    h.runner.environ = environ
    await h.manager.open()
    try:
        started = await h.start(repos=["gitlab.example.net/infra/gitops"])
    finally:
        await h.manager.close()

    assert seen["clone"] == 0, seen["out"]
    # The clone had the full history, read inside the session.
    assert seen["count"] == 0 and seen["shallow"] == "3"
    assert seen["push"] != 0
    assert not any("receive-pack" in str(r.url) for r in host.requests)
    assert h.row(started["id"])["status"] == "done"
    [client] = sdk.clients
    assert "git clone https://gitlab.example.net/infra/gitops" in client.prompt
    # The token is in no variable of the session: its own variable is blank.
    assert options_env_has_no(client.options.env, TOKEN)
    assert seen["env"]["ASSIGNMENTS_GITLAB_TOKEN"] == ""
    assert TOKEN not in "".join(seen["env"].values())
    # The token is in no file under the session HOME, nor anywhere in the scratch.
    assert any(path.name == ".gitconfig" for path in seen["files"])
    for path, data in seen["files"].items():
        assert TOKEN.encode() not in data, path
    gitconfig = seen["gitconfig"]
    assert "insteadOf = https://" in gitconfig
    # The token is in no log line.
    assert TOKEN not in "\n".join(f"{r.getMessage()} {r.args}" for r in caplog.records)
    # The secret of the session is gone with the session.
    assert git.owner("Bearer " + gitconfig.split("Bearer ", 1)[1].split()[0]) is None


def options_env_has_no(env: dict[str, str], value: str) -> bool:
    return all(value not in item for item in env.values())


def read_tree(root: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
