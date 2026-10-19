"""The in-process ``curator`` tool server: list, apply, and dismiss findings,
with a fake finding table. A guest gets nothing."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from curator_fakes import (
    POTHOS,
    SNAKE,
    TOMATOES,
    FakeCuratorStore,
    FakeFindingStore,
    FakeWorkers,
    build_curator_wiki,
)
from jobs_fakes import fixed_clock, make_settings
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.curator import (
    BUSY,
    NOT_AVAILABLE,
    CuratorContext,
    build_curator_server,
    do_apply_finding,
    do_dismiss_finding,
    do_list_findings,
    register,
)
from joshua_core.jobs.curator.job import CuratorJob
from joshua_core.jobs.curator.track import needs_review
from joshua_core.memory import embed as embed_module
from joshua_core.store.models import Channel, Conversation, Person
from joshua_shared import wikigit

ON = "jobs:\n  enabled: true\n  curator:\n    enabled: true\n"


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch):
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [1.0] + [0.0] * 383)


class World:
    def __init__(self, data: Path, git: bool = False) -> None:
        self.data = data
        self.wiki = build_curator_wiki(data)
        self.settings = make_settings(ON + ("" if git else "wiki:\n  git: false\n"))
        self.repo = FakeFindingStore()
        self.repo.people = {
            "alex": Person(id="alex", display_name="Alex", role="member"),
            "gus": Person(id="gus", display_name="Gus", role="guest"),
        }
        self.lock = asyncio.Lock()
        self.ctx = CuratorContext(
            settings=self.settings, data_dir=data, lock=self.lock, clock=fixed_clock()
        )

    async def plan(self) -> None:
        job = CuratorJob(
            store=FakeCuratorStore(),
            findings=self.repo,
            settings=self.settings,
            data_dir=self.data,
            runner=FakeWorkers(),
        )
        await job.run_once(args={}, dry_run=True, clock=fixed_clock())

    def deps(self, person: str | None = "alex") -> ToolDeps:
        conv = Conversation(id="c1", channel_id="telegram:1", person_id=person)
        channel = Channel(id="telegram:1", channel_type="telegram")
        return ToolDeps(
            repo=self.repo, conversation=conv, channel=channel, tz="UTC", person_id=person
        )

    def find(self, kind: str, path: str) -> dict:
        return next(r for r in self.repo.by_kind(kind) if path in r["paths"])


def _git(wiki: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=wiki, capture_output=True, text=True).stdout


def _text(reply: dict) -> str:
    return reply["content"][0]["text"]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path / "data")


async def test_list_findings(world: World) -> None:
    await world.plan()
    text = _text(await do_list_findings(world.deps(), world.ctx))
    assert text.startswith("Curator findings:")
    assert "misplaced (Tier B)" in text and "[open]" in text
    assert "no applied curator findings" in _text(
        await do_list_findings(world.deps(), world.ctx, status="applied")
    )
    assert "must be one of" in _text(await do_list_findings(world.deps(), world.ctx, status="x"))
    some = _text(await do_list_findings(world.deps(), world.ctx, status="all", limit=2))
    assert some.count("\n- #") == 2


async def test_apply_finding_applies_the_stored_patch(world: World) -> None:
    await world.plan()
    row = world.find("misplaced", TOMATOES)
    text = _text(await do_apply_finding(world.deps(), world.ctx, finding_id=row["id"]))
    assert text.startswith(f"Applied finding #{row['id']}.")
    assert (world.wiki / "garden/tomatoes.md").is_file()
    stored = world.repo.rows[row["id"]]
    assert stored["status"] == "applied"
    assert stored["resolved_by"] == "alex"
    assert "Finding" in _text(await do_apply_finding(world.deps(), world.ctx, finding_id=row["id"]))


async def test_apply_finding_refuses_a_guest_and_a_stranger(world: World) -> None:
    await world.plan()
    row = world.find("misplaced", TOMATOES)
    for person in ("gus", None, "nobody"):
        deps = world.deps(person)
        for reply in (
            await do_apply_finding(deps, world.ctx, finding_id=row["id"]),
            await do_list_findings(deps, world.ctx),
            await do_dismiss_finding(deps, world.ctx, finding_id=row["id"], why="no"),
        ):
            assert _text(reply) == NOT_AVAILABLE
    assert (world.wiki / TOMATOES).is_file()
    assert world.repo.rows[row["id"]]["status"] == "open"


async def test_apply_finding_with_no_patch_or_no_row(world: World) -> None:
    await world.plan()
    row = world.find("no-section", "misc/notes.md")
    assert "has no patch" in _text(
        await do_apply_finding(world.deps(), world.ctx, finding_id=row["id"])
    )
    assert "found no curator finding" in _text(
        await do_apply_finding(world.deps(), world.ctx, finding_id=999)
    )
    assert "found no curator finding" in _text(
        await do_apply_finding(world.deps(), world.ctx, finding_id="abc")
    )


async def test_apply_finding_waits_for_no_lock(world: World) -> None:
    await world.plan()
    row = world.find("misplaced", TOMATOES)
    async with world.lock:
        assert _text(await do_apply_finding(world.deps(), world.ctx, finding_id=row["id"])) == BUSY
    assert (world.wiki / TOMATOES).is_file()


async def test_a_patch_that_no_longer_fits_makes_the_finding_stale(world: World) -> None:
    await world.plan()
    row = world.find("refresh", SNAKE)
    page = world.wiki / SNAKE
    page.write_text(page.read_text().replace("every 2 weeks", "every week"))
    text = _text(await do_apply_finding(world.deps(), world.ctx, finding_id=row["id"]))
    assert "I did not apply" in text
    assert world.repo.rows[row["id"]]["status"] == "stale"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")
async def test_apply_finding_commits_and_keeps_clean_pages_clean(tmp_path: Path) -> None:
    world = World(tmp_path / "data", git=True)
    wikigit.ensure_repo(world.wiki)
    wikigit.commit(world.wiki, None, "start")
    await world.plan()
    row = world.find("broken-link", POTHOS)
    await do_apply_finding(world.deps(), world.ctx, finding_id=row["id"])
    log = _git(world.wiki, "log", "-1", "--format=%s").strip()
    assert log == f"curator: finding {row['id']}"
    status = _git(world.wiki, "status", "--porcelain")
    assert POTHOS not in status
    # The page had no tracking keys, so it gets none from a chat apply.
    assert needs_review((world.wiki / POTHOS).read_text())


async def test_dismiss_finding(world: World) -> None:
    await world.plan()
    row = world.find("orphan", "misc/notes.md")
    assert "Tell me why" in _text(
        await do_dismiss_finding(world.deps(), world.ctx, finding_id=row["id"], why=" ")
    )
    text = _text(
        await do_dismiss_finding(world.deps(), world.ctx, finding_id=row["id"], why="fine")
    )
    assert text.startswith(f"Dismissed finding #{row['id']}")
    stored = world.repo.rows[row["id"]]
    assert stored["status"] == "dismissed"
    assert stored["detail"]["note"] == "fine"
    assert stored["resolved_by"] == "alex"
    again = await do_dismiss_finding(world.deps(), world.ctx, finding_id=row["id"], why="x")
    assert "I dismiss only an open finding" in _text(again)
    missing = await do_dismiss_finding(world.deps(), world.ctx, finding_id=0, why="x")
    assert "found no curator finding" in _text(missing)


def test_build_curator_server(world: World) -> None:
    server = build_curator_server(world.deps(), world.ctx)
    assert server["type"] == "sdk"
    assert server["name"] == "curator"


def test_register_needs_both_switches(tmp_path: Path) -> None:
    def manager(wired: dict):
        return type("M", (), {"register_builtin": lambda self, n, f: wired.__setitem__(n, f)})()

    lock = asyncio.Lock()
    for extra, expected in (
        ("", False),
        ("jobs:\n  enabled: true\n", False),
        ("jobs:\n  curator:\n    enabled: true\n", False),
        (ON, True),
    ):
        wired: dict = {}
        got = register(manager(wired), settings=make_settings(extra), data_dir=tmp_path, lock=lock)
        assert got is expected
        assert ("curator" in wired) is expected
