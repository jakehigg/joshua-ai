"""The `snooze_insight` tool, the due rule, and the ranked list of
`list_insights`. A guest cannot snooze."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from insights_fakes import insights_settings
from jobs_fakes import FakeJobRunStore, fixed_clock
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.insights import (
    BUSY,
    LIST_HEADER,
    NOT_AVAILABLE,
    InsightsContext,
    do_list_insights,
    do_snooze_insight,
    snooze_length,
)
from joshua_core.jobs import JobRunner
from joshua_core.jobs.insights.page import load_insight, render_index, set_fields
from joshua_core.store.models import Channel, Conversation, Person
from joshua_shared import layout, wikigit
from research_fakes import snapshot
from snooze_fakes import TODAY, write_insight


class People:
    def __init__(self) -> None:
        self.people = {
            "alex": Person(id="alex", display_name="Alex", role="member"),
            "gus": Person(id="gus", display_name="Gus", role="guest"),
        }

    async def get_person(self, pid: str) -> Any:
        return self.people.get(pid)


class World:
    def __init__(self, data: Path, extra: str = "wiki:\n  git: false\n") -> None:
        self.data = data
        layout.wiki_root(data).mkdir(parents=True, exist_ok=True)
        self.settings = insights_settings(extra)
        self.lock = asyncio.Lock()
        jobs = JobRunner(
            repo=FakeJobRunStore(), settings=self.settings, data_dir=data, jobs=[], lock=self.lock
        )
        self.ctx = InsightsContext(
            settings=self.settings, data_dir=data, lock=self.lock, jobs=jobs, clock=fixed_clock()
        )

    def deps(self, person: str | None = "alex") -> ToolDeps:
        return ToolDeps(
            repo=People(),  # type: ignore[arg-type]
            conversation=Conversation(id="c1", channel_id="telegram:1", person_id=person),
            channel=Channel(id="telegram:1", channel_type="telegram"),
            tz="UTC",
            person_id=person,
        )

    async def snooze(self, insight_id: str, days: Any = None, person: str = "alex") -> str:
        reply = await do_snooze_insight(
            self.deps(person), self.ctx, insight_id=insight_id, days=days
        )
        return reply["content"][0]["text"]

    async def listing(self, status: str = "open") -> str:
        reply = await do_list_insights(self.deps(), self.ctx, status=status)
        return reply["content"][0]["text"]


@pytest.fixture
def w(tmp_path: Path) -> World:
    return World(tmp_path / "data")


def _iso(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


# --- snooze ------------------------------------------------------------------


async def test_a_snooze_with_no_days_uses_snooze_days(w: World) -> None:
    slug = write_insight(w.data, "backup-check")
    reply = await w.snooze(slug)
    assert reply == (f"Insight `{slug}` is snoozed until {_iso(6)}. It comes back on {_iso(7)}.")
    found = load_insight(slug, w.data)
    assert found is not None
    assert found.insight.snoozed_until == TODAY + timedelta(days=6)
    assert found.insight.snoozes == 1
    assert found.insight.resolved_at is None and found.insight.resolved_by is None


async def test_each_later_snooze_doubles_up_to_the_cap(w: World) -> None:
    slug = write_insight(w.data, "backup-check")
    lengths = []
    for _ in range(6):
        before = load_insight(slug, w.data)
        assert before is not None
        await w.snooze(slug)
        after = load_insight(slug, w.data)
        assert after is not None and after.insight.snoozed_until is not None
        lengths.append((after.insight.snoozed_until - TODAY).days + 1)
    assert lengths == [7, 14, 28, 56, 90, 90]
    assert load_insight(slug, w.data).insight.snoozes == 6  # type: ignore[union-attr]


def test_the_snooze_length_rule() -> None:
    settings = insights_settings()
    settings.jobs.insights.snooze_days = 3
    settings.jobs.insights.snooze_max_days = 20
    assert [snooze_length(settings, n, None) for n in range(5)] == [3, 6, 12, 20, 20]
    assert snooze_length(settings, 0, 5) == 5
    assert snooze_length(settings, 4, 5) == 5
    assert snooze_length(settings, 0, 0) == 1
    assert snooze_length(settings, 0, -4) == 1
    assert snooze_length(settings, 0, 400) == 20
    assert snooze_length(settings, 10_000, None) == 20


async def test_explicit_days_are_used_and_clamped(w: World) -> None:
    slug = write_insight(w.data, "backup-check", snoozes=3)
    assert f"snoozed until {_iso(1)}" in await w.snooze(slug, days=2)
    assert f"snoozed until {_iso(89)}" in await w.snooze(slug, days="500")
    assert f"snoozed until {_iso(0)}" in await w.snooze(slug, days=0)
    assert "whole number" in await w.snooze(slug, days="soon")
    assert "whole number" in await w.snooze(slug, days=1.5)
    assert load_insight(slug, w.data).insight.snoozes == 6  # type: ignore[union-attr]


async def test_a_new_insight_becomes_acknowledged(w: World) -> None:
    new = write_insight(w.data, "filter-change")
    told = write_insight(w.data, "smoke-alarm", status="told")
    await w.snooze(new)
    await w.snooze(told)
    assert load_insight(new, w.data).insight.status == "acknowledged"  # type: ignore[union-attr]
    assert load_insight(told, w.data).insight.status == "told"  # type: ignore[union-attr]


@pytest.mark.parametrize("status", ["dismissed", "expired"])
async def test_a_closed_insight_cannot_be_snoozed(w: World, status: str) -> None:
    slug = write_insight(w.data, "backup-check", status=status)
    before = snapshot(layout.wiki_root(w.data))
    assert f"is {status}, so I did not snooze it" in await w.snooze(slug)
    assert snapshot(layout.wiki_root(w.data)) == before


async def test_an_unknown_insight_and_the_lock(w: World) -> None:
    assert "I found no insight" in await w.snooze("home/nope")
    assert "I found no insight" in await w.snooze("../../people/alex")
    slug = write_insight(w.data, "backup-check")
    async with w.lock:
        assert await w.snooze(slug) == BUSY
    assert load_insight(slug, w.data).insight.snoozes == 0  # type: ignore[union-attr]


@pytest.mark.parametrize("person", ["gus", None, "stranger"])
async def test_a_guest_cannot_snooze(w: World, person: str | None) -> None:
    slug = write_insight(w.data, "backup-check")
    before = snapshot(layout.wiki_root(w.data))
    reply = await do_snooze_insight(w.deps(person), w.ctx, insight_id=slug, days=3)
    assert reply["content"][0]["text"] == NOT_AVAILABLE
    assert snapshot(layout.wiki_root(w.data)) == before


async def test_the_readme_row_shows_the_snooze(w: World) -> None:
    slug = write_insight(w.data, "backup-check")
    await w.snooze(slug, days=3)
    index = layout.insights_index(w.data).read_text()
    assert f"| acknowledged, snoozed until {_iso(2)} | note |" in index
    found = load_insight(slug, w.data)
    assert found is not None
    closed = set_fields(found, w.data, status="dismissed")
    assert "snoozed until" not in render_index([closed])


def _last_commit(wiki: Path) -> str:
    return subprocess.run(
        ["git", "show", "--name-only", "--format=%s", "HEAD"],
        cwd=wiki,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="needs the git binary")
async def test_a_snooze_commits_the_wiki(tmp_path: Path) -> None:
    w = World(tmp_path / "data", extra="")
    wiki = layout.wiki_root(w.data)
    wikigit.ensure_repo(wiki)
    wikigit.commit(wiki, None, "start")
    slug = write_insight(w.data, "backup-check")
    await w.snooze(slug, days=4)
    out = _last_commit(wiki)
    assert out.startswith(f"insights: {slug} snoozed until {_iso(3)} by alex")
    assert f"insights/{slug}.md" in out and "insights/README.md" in out


# --- the due rule ------------------------------------------------------------


def test_a_snooze_that_ended_yesterday_is_due_today(tmp_path: Path) -> None:
    data = tmp_path / "data"
    ended = load_insight(write_insight(data, "a", snoozed_until=TODAY - timedelta(days=1)), data)
    last_day = load_insight(write_insight(data, "b", snoozed_until=TODAY), data)
    assert ended is not None and last_day is not None
    assert ended.insight.is_due(TODAY) and not ended.insight.is_snoozed(TODAY)
    assert last_day.insight.is_snoozed(TODAY) and not last_day.insight.is_due(TODAY)
    assert last_day.insight.is_due(TODAY + timedelta(days=1))


# --- the ranked list ---------------------------------------------------------


async def test_the_list_is_ranked(w: World) -> None:
    old_note = write_insight(w.data, "old-note", found_at=TODAY - timedelta(days=40))
    new_note = write_insight(w.data, "new-note", found_at=TODAY - timedelta(days=2))
    act_low = write_insight(w.data, "act-low", severity="act", confidence="low")
    act_high = write_insight(w.data, "act-high", severity="act", confidence="high")
    urgent = write_insight(w.data, "urgent-one", severity="urgent", confidence="low")
    text = await w.listing()
    lines = text.splitlines()
    assert lines[0] == LIST_HEADER
    assert LIST_HEADER == (
        "Ranked by severity, then confidence. Name every urgent and act item when you summarize."
    )
    order = [urgent, act_high, act_low, old_note, new_note]
    assert [line.split("`")[1] for line in lines[1:]] == order
    assert f"- `{urgent}` [new, urgent] The urgent one is not done." in lines[1]


async def test_open_shows_only_due_and_snoozed_shows_the_rest(w: World) -> None:
    due = write_insight(w.data, "filter-change", snoozed_until=TODAY - timedelta(days=1))
    later = write_insight(
        w.data, "backup-check", status="acknowledged", snoozed_until=TODAY + timedelta(days=5)
    )
    write_insight(
        w.data, "smoke-alarm", status="dismissed", snoozed_until=TODAY + timedelta(days=5)
    )
    open_text = await w.listing()
    assert due in open_text and later not in open_text and "smoke-alarm" not in open_text
    snoozed = await w.listing("snoozed")
    assert snoozed.splitlines()[0] == LIST_HEADER
    assert later in snoozed and due not in snoozed and "smoke-alarm" not in snoozed
    assert f"snoozed until {_iso(5)}" in snoozed
    every = await w.listing("all")
    lines = every.splitlines()[1:]
    assert [line.split("`")[1] for line in lines] == [later, due, "home/smoke-alarm"]


async def test_no_snoozed_insights(w: World) -> None:
    write_insight(w.data, "filter-change")
    assert await w.listing("snoozed") == "There are no snoozed insights."


async def test_the_server_has_the_snooze_tool(w: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from joshua_core.engine.tools import insights as insights_module

    monkeypatch.setattr(insights_module, "create_sdk_mcp_server", lambda **kw: kw)
    server = insights_module.build_insights_server(w.deps(), w.ctx)
    tools = {t.name: t for t in server["tools"]}
    assert "snooze_insight" in tools
    text = tools["snooze_insight"].description
    assert "not now, later, or that they are busy" in text and "dismiss_insight" in text
    slug = write_insight(w.data, "backup-check")
    reply = await tools["snooze_insight"].handler({"slug": slug, "days": 2})
    assert f"snoozed until {_iso(1)}" in reply["content"][0]["text"]
