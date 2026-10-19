"""The in-process ``research`` tool server: plan, run, queue a question, and the
status. A guest gets nothing from any tool."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from jobs_fakes import FakeJobRunStore, fixed_clock
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.research import (
    BUSY,
    NOT_AVAILABLE,
    ResearchContext,
    build_research_server,
    do_plan_research,
    do_queue_question,
    do_research_status,
    do_run_research,
    register,
)
from joshua_core.jobs import JobRunner
from joshua_core.jobs.research.dossier import load_dossier
from joshua_core.jobs.research.job import ResearchJob
from joshua_core.memory import embed as embed_module
from joshua_core.store.models import Channel, Conversation, Person
from joshua_shared import layout, wikigit
from research_fakes import (
    GROUNDED,
    GROUNDED_PHRASES,
    SNAKE,
    FakeInternet,
    FakeStore,
    FakeWorkers,
    build_research_wiki,
    raised,
    research_settings,
    snapshot,
    word_vector,
)


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": word_vector(text))


class People:
    def __init__(self) -> None:
        self.people = {
            "alex": Person(id="alex", display_name="Alex", role="member"),
            "gus": Person(id="gus", display_name="Gus", role="guest"),
        }

    async def get_person(self, pid: str) -> Any:
        return self.people.get(pid)


class World:
    def __init__(self, data: Path, git: bool = False) -> None:
        self.data = data
        self.wiki = build_research_wiki(data)
        self.settings = research_settings("" if git else "wiki:\n  git: false\n")
        self.lock = asyncio.Lock()
        self.runs = FakeJobRunStore()
        job = ResearchJob(
            store=FakeStore(),
            settings=self.settings,
            data_dir=data,
            runner=FakeWorkers(raise_answer=raised((GROUNDED, GROUNDED_PHRASES))),
            internet_runner=FakeInternet(),
        )
        self.jobs = JobRunner(
            repo=self.runs,
            settings=self.settings,
            data_dir=data,
            jobs=[job],
            lock=self.lock,
            clock=fixed_clock(),
        )
        self.ctx = ResearchContext(
            settings=self.settings,
            data_dir=data,
            lock=self.lock,
            jobs=self.jobs,
            clock=fixed_clock(),
        )
        self.repo = People()

    def deps(self, person: str | None = "alex") -> ToolDeps:
        conv = Conversation(id="c1", channel_id="telegram:1", person_id=person)
        channel = Channel(id="telegram:1", channel_type="telegram")
        return ToolDeps(
            repo=self.repo,  # type: ignore[arg-type]
            conversation=conv,
            channel=channel,
            tz="UTC",
            person_id=person,
        )


def _last_commit(wiki: Path) -> str:
    out = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=wiki, capture_output=True, text=True
    )
    return out.stdout.strip()


def _text(reply: dict) -> str:
    return reply["content"][0]["text"]


def _run_id(text: str) -> int:
    return int(text.split("research run ")[1].split(" ")[0])


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path / "data")


async def test_plan_research_starts_a_plan_run(world: World) -> None:
    text = _text(await do_plan_research(world.deps(), world.ctx, topic=SNAKE))
    assert text.startswith("Started research run")
    run_id = _run_id(text)
    await world.jobs.wait(run_id)
    row = world.runs.rows[run_id]
    assert row["dry_run"] is False
    assert row["args"] == {"mode": "plan", "topic": SNAKE}
    assert row["report"]["data"]["topic"] == SNAKE
    status = _text(await do_research_status(world.deps(), world.ctx))
    assert status.startswith(f"Research run {run_id}: research plan for {SNAKE}")


async def test_run_research_starts_a_run(world: World) -> None:
    text = _text(
        await do_run_research(world.deps(), world.ctx, topic=SNAKE, questions=None, max_questions=1)
    )
    run_id = _run_id(text)
    await world.jobs.wait(run_id)
    row = world.runs.rows[run_id]
    assert row["args"] == {"mode": "run", "topic": SNAKE, "max_questions": 1}
    assert len(row["report"]["data"]["pages"]) == 1


async def test_a_bad_argument_and_a_busy_lock_start_nothing(world: World) -> None:
    text = _text(await do_run_research(world.deps(), world.ctx, topic="nope.md"))
    assert text.startswith("I did not start the research: not a topic page")
    await world.lock.acquire()
    try:
        assert _text(await do_plan_research(world.deps(), world.ctx)) == BUSY
        reply = await do_queue_question(
            world.deps(), world.ctx, topic=SNAKE, question="Is the snake plant safe for a cat?"
        )
        assert _text(reply) == BUSY
    finally:
        world.lock.release()
    assert world.runs.rows == {}


async def test_status_before_any_run(world: World) -> None:
    assert _text(await do_research_status(world.deps(), world.ctx)) == (
        "The researcher has not run yet."
    )


async def test_queue_question_adds_a_person_question(world: World) -> None:
    reply = await do_queue_question(
        world.deps(), world.ctx, topic=SNAKE, question=" Is the snake plant safe for a cat? "
    )
    assert "Queued the question as `snake-plant-safe-cat`" in _text(reply)
    dossier = load_dossier(SNAKE, world.data)
    assert dossier is not None
    question = dossier.questions[0]
    assert question.asked_by == "alex"
    assert question.status == "open"
    assert question.grounded_in == []
    assert str(question.raised_at) == "2026-10-19"
    assert layout.knowledge_index(world.data).is_file()


async def test_queue_question_checks_the_length_and_the_topic_name(world: World) -> None:
    short = await do_queue_question(world.deps(), world.ctx, topic=SNAKE, question="Cat?")
    assert "characters" in _text(short)
    other = await do_queue_question(
        world.deps(), world.ctx, topic=SNAKE, question="Is this plant safe for a cat?"
    )
    assert "does not name the topic" in _text(other)
    page = await do_queue_question(
        world.deps(), world.ctx, topic="houseplants.md", question="Is the snake plant ok?"
    )
    assert "is not a topic page" in _text(page)
    assert not layout.knowledge_root(world.data).exists()


async def test_a_guest_is_refused_by_every_tool(world: World) -> None:
    before = snapshot(world.wiki)
    for person in ("gus", None, "stranger"):
        deps = world.deps(person)
        assert _text(await do_plan_research(deps, world.ctx)) == NOT_AVAILABLE
        assert _text(await do_run_research(deps, world.ctx, topic=SNAKE)) == NOT_AVAILABLE
        reply = await do_queue_question(
            deps, world.ctx, topic=SNAKE, question="Is the snake plant safe for a cat?"
        )
        assert _text(reply) == NOT_AVAILABLE
        assert _text(await do_research_status(deps, world.ctx)) == NOT_AVAILABLE
    assert snapshot(world.wiki) == before
    assert world.runs.rows == {}


@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")
async def test_queue_question_commits(tmp_path: Path) -> None:
    world = World(tmp_path / "data", git=True)
    wikigit.ensure_repo(world.wiki)
    wikigit.commit(world.wiki, None, "start")
    await do_queue_question(
        world.deps(), world.ctx, topic=SNAKE, question="Is the snake plant safe for a cat?"
    )
    log = _last_commit(world.wiki)
    assert log == "research: question from alex"


def test_build_research_server(world: World) -> None:
    server = build_research_server(world.deps(), world.ctx)
    assert server["type"] == "sdk"
    assert server["name"] == "research"


def test_register_needs_both_switches(tmp_path: Path, world: World) -> None:
    def manager(wired: dict):
        return type("M", (), {"register_builtin": lambda self, n, f: wired.__setitem__(n, f)})()

    from jobs_fakes import make_settings

    for settings, expected in (
        (make_settings(), False),
        (make_settings("jobs:\n  enabled: true\n"), False),
        (make_settings("jobs:\n  research:\n    enabled: true\n"), False),
        (research_settings(), True),
    ):
        wired: dict = {}
        got = register(
            manager(wired), settings=settings, data_dir=tmp_path, lock=world.lock, jobs=world.jobs
        )
        assert got is expected
        assert ("research" in wired) is expected
