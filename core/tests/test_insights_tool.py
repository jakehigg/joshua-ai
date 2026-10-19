"""The in-process ``insights`` tool server: list, acknowledge, dismiss, and run.
A guest gets nothing from any tool."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from insights_fakes import (
    IRRIGATION,
    ROSES,
    FakeInsightWorker,
    build_insights_wiki,
    claim_vector,
    insights_settings,
)
from jobs_fakes import FakeJobRunStore, FakeStore, fixed_clock
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.insights import (
    BUSY,
    NOT_AVAILABLE,
    InsightsContext,
    build_insights_server,
    do_acknowledge_insight,
    do_dismiss_insight,
    do_list_insights,
    do_run_insights,
    register,
)
from joshua_core.jobs import JobRunner
from joshua_core.jobs.insights.job import InsightsJob
from joshua_core.jobs.insights.page import load_insight, load_insights, set_fields
from joshua_core.memory import embed as embed_module
from joshua_core.store.models import Channel, Conversation, Person
from joshua_shared import layout, wikigit
from research_fakes import snapshot


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": claim_vector(text))


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
        self.wiki = build_insights_wiki(data)
        self.settings = insights_settings("" if git else "wiki:\n  git: false\n")
        self.lock = asyncio.Lock()
        self.runs = FakeJobRunStore()
        self.job = InsightsJob(
            store=FakeStore(), settings=self.settings, data_dir=data, runner=FakeInsightWorker()
        )
        self.jobs = JobRunner(
            repo=self.runs,
            settings=self.settings,
            data_dir=data,
            jobs=[self.job],
            lock=self.lock,
            clock=fixed_clock(),
        )
        self.ctx = InsightsContext(
            settings=self.settings,
            data_dir=data,
            lock=self.lock,
            jobs=self.jobs,
            clock=fixed_clock(),
        )
        self.repo = People()

    async def seed(self) -> str:
        """Run the job once, so one insight is on the disk, and give its id."""
        await self.job.run_once(args={"topic": IRRIGATION}, dry_run=False, clock=fixed_clock())
        [found] = load_insights(self.data)
        return found.id

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


def _text(reply: dict) -> str:
    return reply["content"][0]["text"]


@pytest.fixture
def w(tmp_path: Path) -> World:
    return World(tmp_path / "data")


async def test_list_insights(w: World) -> None:
    assert "There are no open insights" in _text(await do_list_insights(w.deps(), w.ctx))
    slug = await w.seed()
    text = _text(await do_list_insights(w.deps(), w.ctx))
    assert f"`{slug}` [new, act]" in text and IRRIGATION in text
    assert slug in _text(await do_list_insights(w.deps(), w.ctx, topic=IRRIGATION))
    assert "no open" in _text(await do_list_insights(w.deps(), w.ctx, topic=ROSES))
    assert "no dismissed" in _text(await do_list_insights(w.deps(), w.ctx, status="dismissed"))
    assert slug in _text(await do_list_insights(w.deps(), w.ctx, status="all", limit="x"))
    assert "must be one of" in _text(await do_list_insights(w.deps(), w.ctx, status="bogus"))


async def test_acknowledge_insight(w: World) -> None:
    slug = await w.seed()
    reply = _text(await do_acknowledge_insight(w.deps(), w.ctx, insight_id=f"{slug}.md"))
    assert reply == f"Insight `{slug}` is acknowledged now."
    found = load_insight(slug, w.data)
    assert found is not None and found.insight.status == "acknowledged"
    assert found.insight.resolved_by == "alex"
    assert found.insight.resolved_at is not None
    assert "| acknowledged |" in layout.insights_index(w.data).read_text()
    again = _text(await do_acknowledge_insight(w.deps(), w.ctx, insight_id=slug))
    assert "is acknowledged, so I did not change it" in again


async def test_dismiss_insight(w: World) -> None:
    slug = await w.seed()
    assert "Tell me why" in _text(
        await do_dismiss_insight(w.deps(), w.ctx, insight_id=slug, why="")
    )
    reply = _text(
        await do_dismiss_insight(w.deps(), w.ctx, insight_id=slug, why="We use drip now.")
    )
    assert "dismissed now" in reply
    found = load_insight(slug, w.data)
    assert found is not None and found.insight.status == "dismissed"
    assert found.insight.resolution_note == "We use drip now."
    assert found.insight.resolved_by == "alex"
    assert "no open" in _text(await do_list_insights(w.deps(), w.ctx))


async def test_an_unknown_or_bad_slug(w: World) -> None:
    for bad in ("garden/nope", "../../people/alex", "nope", ""):
        assert "I found no insight" in _text(
            await do_acknowledge_insight(w.deps(), w.ctx, insight_id=bad)
        )


async def test_a_status_change_waits_for_the_lock(w: World) -> None:
    slug = await w.seed()
    async with w.lock:
        assert _text(await do_acknowledge_insight(w.deps(), w.ctx, insight_id=slug)) == BUSY
    assert load_insight(slug, w.data).insight.status == "new"  # type: ignore[union-attr]


async def test_an_expired_insight_cannot_be_dismissed(w: World) -> None:
    slug = await w.seed()
    found = load_insight(slug, w.data)
    assert found is not None
    set_fields(found, w.data, status="expired")
    reply = _text(await do_dismiss_insight(w.deps(), w.ctx, insight_id=slug, why="x"))
    assert "is expired" in reply


async def test_run_insights(w: World) -> None:
    reply = _text(await do_run_insights(w.deps(), w.ctx, topic=IRRIGATION))
    assert reply.startswith("Started insights run 1.")
    await w.jobs.wait(1)
    row = w.runs.rows[1]
    assert row["dry_run"] is False and row["args"] == {"topic": IRRIGATION}
    assert row["report"]["summary"].startswith("insights: garden/irrigation.md (1 new")
    bad = _text(await do_run_insights(w.deps(), w.ctx, topic="garden.md"))
    assert "did not start" in bad
    async with w.lock:
        assert _text(await do_run_insights(w.deps(), w.ctx, topic=IRRIGATION)) == BUSY


async def test_run_insights_with_no_job(tmp_path: Path) -> None:
    w = World(tmp_path / "data")
    jobs = JobRunner(
        repo=FakeJobRunStore(), settings=w.settings, data_dir=w.data, jobs=[], lock=w.lock
    )
    ctx = InsightsContext(settings=w.settings, data_dir=w.data, lock=w.lock, jobs=jobs)
    assert "not turned on" in _text(await do_run_insights(w.deps(), ctx, topic=IRRIGATION))


@pytest.mark.parametrize("person", ["gus", None, "stranger"])
async def test_a_guest_is_refused_by_each_tool(w: World, person: str | None) -> None:
    slug = await w.seed()
    before = snapshot(w.wiki)
    deps = w.deps(person)
    for reply in (
        await do_list_insights(deps, w.ctx),
        await do_acknowledge_insight(deps, w.ctx, insight_id=slug),
        await do_dismiss_insight(deps, w.ctx, insight_id=slug, why="no"),
        await do_run_insights(deps, w.ctx, topic=IRRIGATION),
    ):
        assert _text(reply) == NOT_AVAILABLE
    assert snapshot(w.wiki) == before and len(w.runs.rows) == 0


def _last_commit(wiki: Path) -> str:
    return subprocess.run(
        ["git", "show", "--name-only", "--format=%s", "HEAD"],
        cwd=wiki,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")
async def test_a_status_change_commits_the_wiki(tmp_path: Path) -> None:
    w = World(tmp_path / "data", git=True)
    wikigit.ensure_repo(w.wiki)
    wikigit.commit(w.wiki, None, "start")
    slug = await w.seed()
    await do_dismiss_insight(w.deps(), w.ctx, insight_id=slug, why="We use drip now.")
    out = _last_commit(w.wiki)
    assert out.startswith(f"insights: {slug} dismissed by alex")
    assert f"insights/{slug}.md" in out and "insights/README.md" in out


def test_the_server_and_register() -> None:
    settings = insights_settings()

    class Manager:
        def __init__(self) -> None:
            self.names: list[str] = []

        def register_builtin(self, name: str, factory: Any) -> None:
            self.names.append(name)

    lock = asyncio.Lock()
    jobs = JobRunner(repo=FakeJobRunStore(), settings=settings, data_dir="/x", jobs=[], lock=lock)
    manager = Manager()
    assert register(manager, settings=settings, data_dir="/x", lock=lock, jobs=jobs)
    assert manager.names == ["insights"]
    off = insights_settings()
    off.jobs.insights.enabled = False
    assert not register(Manager(), settings=off, data_dir="/x", lock=lock, jobs=jobs)
    ctx = InsightsContext(settings=settings, data_dir=Path("/x"), lock=lock, jobs=jobs)
    deps = ToolDeps(
        repo=People(),  # type: ignore[arg-type]
        conversation=Conversation(id="c", channel_id="t:1", person_id="alex"),
        channel=Channel(id="t:1", channel_type="telegram"),
        tz="UTC",
        person_id="alex",
    )
    assert build_insights_server(deps, ctx) is not None
