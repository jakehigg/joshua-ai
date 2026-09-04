"""Offline tests for the nightly reflection (canned oneshot, no DB, no SDK)."""

from __future__ import annotations

import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from joshua_core.memory.nightly import NightlyReflector
from joshua_core.store.models import Person
from joshua_shared import layout, wikigit
from nightly_fakes import (
    FakeIndexer,
    FakeManager,
    FakeReflectRepo,
    canned_oneshot,
    junk_oneshot,
    make_settings,
    row,
)

_GIT_MISSING = shutil.which("git") is None

NY = ZoneInfo("America/New_York")
DAY = date(2026, 8, 26)
T10 = datetime(2026, 8, 26, 10, 0, tzinfo=NY)
T11 = datetime(2026, 8, 26, 11, 0, tzinfo=NY)
LONG = "I switched jobs today and we are moving to Boston next month. " * 4

PEOPLE = [
    Person(id="alex", display_name="Alex"),
    Person(id="gwen", display_name="Gwen", role="guest"),
]


def _bootstrap(root: Path) -> None:
    layout.bootstrap_wiki(root)
    layout.bootstrap_shared(root)
    layout.bootstrap_shared_profile("Test House", root)
    layout.bootstrap_person("alex", "Alex", root)
    layout.bootstrap_person("gwen", "Gwen", root)


def _reflector(tmp_path: Path, repo: FakeReflectRepo, *, oneshot=None, extra: str = "") -> tuple:
    indexer, manager = FakeIndexer(), FakeManager()
    reflector = NightlyReflector(
        repo=repo,
        indexer=indexer,
        manager=manager,
        settings=make_settings(extra),
        data_dir=tmp_path,
        oneshot=oneshot or canned_oneshot(),
    )
    return reflector, indexer, manager


def _page(root: Path, d: date = DAY) -> Path:
    return layout.journal_day_page(d, root)


async def test_writes_page_and_profile(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=[row("c1", "in", LONG, T10), row("c1", "out", "Congratulations!", T11)],
        conv_meta={
            "c1": {"session_mode": "per_person", "person_id": "alex", "display_name": "Alex"}
        },
    )
    reflector, indexer, _ = _reflector(tmp_path, repo)

    summary = await reflector.reflect(target_date=DAY)

    page = _page(tmp_path)
    assert page.exists()
    text = page.read_text()
    assert text.startswith("---\ndate: 2026-08-26\npeople: [alex]\nsource: nightly\n---\n")
    assert "Alex had a good day." in text
    assert "Likes tea." in layout.profile_path("alex", tmp_path).read_text()
    assert {"source": "files", "person": None} in indexer.calls
    assert summary["posts_written"] == 1
    assert summary["profiles_updated"] == 1


async def test_no_page_when_the_day_is_not_worth_one(tmp_path: Path) -> None:
    """A day of chatter clears ``min_chars_for_post`` but is still worth no
    page. ``"post": null`` writes no file, and the run is not an error."""
    _bootstrap(tmp_path)
    chatter = "Turn the porch light on, start the dishwasher, and good night. " * 4
    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=[row("c1", "in", chatter, T10), row("c1", "out", "Done, good night.", T11)],
        conv_meta={
            "c1": {"session_mode": "per_person", "person_id": "alex", "display_name": "Alex"}
        },
    )
    reflector, _, _ = _reflector(tmp_path, repo, oneshot=canned_oneshot(post=None))
    summary = await reflector.reflect(target_date=DAY)
    assert summary["posts_written"] == 0
    assert summary["errors"] == 0
    assert not _page(tmp_path).exists()


async def test_short_day_writes_nothing(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    before = layout.profile_path("alex", tmp_path).read_text()
    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=[row("c1", "in", "hi", T10)],
        conv_meta={
            "c1": {"session_mode": "per_person", "person_id": "alex", "display_name": "Alex"}
        },
    )
    reflector, indexer, _ = _reflector(tmp_path, repo)

    summary = await reflector.reflect(target_date=DAY)

    assert not _page(tmp_path).exists()
    assert layout.profile_path("alex", tmp_path).read_text() == before
    assert indexer.calls == []
    assert summary["posts_written"] == 0


async def test_rerun_overwrites_the_page(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=[row("c1", "in", LONG, T10)],
        conv_meta={
            "c1": {"session_mode": "per_person", "person_id": "alex", "display_name": "Alex"}
        },
    )
    reflector, _, _ = _reflector(tmp_path, repo)
    await reflector.reflect(target_date=DAY)

    reflector2, _, _ = _reflector(
        tmp_path, repo, oneshot=canned_oneshot(post="A different second entry.", profile=None)
    )
    await reflector2.reflect(target_date=DAY)

    assert "A different second entry." in _page(tmp_path).read_text()


async def test_guest_dm_gets_a_page(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=[row("g1", "in", LONG, T10)],
        conv_meta={
            "g1": {"session_mode": "per_person", "person_id": "gwen", "display_name": "Gwen"}
        },
    )
    reflector, _, _ = _reflector(tmp_path, repo)

    await reflector.reflect(target_date=DAY)

    text = _page(tmp_path).read_text()
    assert "people: [gwen]" in text


async def test_group_attributes_member_not_guest(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    rows = [
        row("grp", "in", LONG, T10, turn_id="t1", speaker="Alex"),
        row("grp", "out", "Nice, Alex.", T10, turn_id="t1"),
        row("grp", "in", LONG, T11, turn_id="t2", speaker="Gwen"),
        row("grp", "out", "Hello Gwen.", T11, turn_id="t2"),
    ]
    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=rows,
        conv_meta={"grp": {"session_mode": "shared", "person_id": None, "display_name": None}},
    )
    reflector, indexer, _ = _reflector(tmp_path, repo)

    summary = await reflector.reflect(target_date=DAY)

    text = _page(tmp_path).read_text()
    # Only the member is in the day's `people` front matter, but the group
    # transcript (including the guest's turns) still fed the one call.
    assert "people: [alex]" in text
    # The shared pass ran over the group chat.
    assert summary["shared_updated"] is True
    assert "Quiet weeknights." in layout.shared_profile_path(tmp_path).read_text()
    assert {"source": "files", "person": None} in indexer.calls


async def test_unparseable_reflection_skips_the_day(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=[row("c1", "in", LONG, T10)],
        conv_meta={
            "c1": {"session_mode": "per_person", "person_id": "alex", "display_name": "Alex"}
        },
    )
    reflector, _, _ = _reflector(tmp_path, repo, oneshot=junk_oneshot())

    summary = await reflector.reflect(target_date=DAY)

    assert not _page(tmp_path).exists()
    assert summary["errors"] >= 1
    assert summary["posts_written"] == 0


async def test_skill_teaching_exchange_is_dropped(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=[
            row("c1", "in", LONG, T10, turn_id="t1"),
            row(
                "c1",
                "out",
                "Saved the skill.",
                T10,
                turn_id="t1",
                tools=["mcp__files__write_file"],
                written=["wiki/skills/movie-time.md"],
            ),
        ],
        conv_meta={
            "c1": {"session_mode": "per_person", "person_id": "alex", "display_name": "Alex"}
        },
    )
    reflector, _, _ = _reflector(tmp_path, repo)

    await reflector.reflect(target_date=DAY)

    assert not _page(tmp_path).exists()


async def test_a_journaled_day_still_reflects(tmp_path: Path) -> None:
    """A regression: the agent journals with `write_journal_entry` during an
    ordinary conversation, so keying the skip on the tool name threw the day
    away and `profile.md` was never written."""
    _bootstrap(tmp_path)
    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=[
            row("c1", "in", LONG, T10),
            row(
                "c1",
                "out",
                "Noted in your journal.",
                T11,
                tools=["mcp__files__write_journal_entry"],
                written=["wiki/journal/2026/08/26/moving-to-boston.md"],
            ),
        ],
        conv_meta={
            "c1": {"session_mode": "per_person", "person_id": "alex", "display_name": "Alex"}
        },
    )
    reflector, _, _ = _reflector(tmp_path, repo)

    summary = await reflector.reflect(target_date=DAY)

    assert summary["posts_written"] == 1
    assert summary["profiles_updated"] == 1
    assert summary["people_reflected"] == 1
    assert "Likes tea." in layout.profile_path("alex", tmp_path).read_text()


async def test_a_wiki_write_is_not_skill_teaching(tmp_path: Path) -> None:
    """A reply that writes an ordinary wiki page is work, not teaching, so the
    exchange stays in the day."""
    _bootstrap(tmp_path)
    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=[
            row("c1", "in", LONG, T10),
            row(
                "c1",
                "out",
                "Saved that as a skill.",
                T11,
                tools=["mcp__files__write_file"],
                written=["wiki/boston-move.md"],
            ),
        ],
        conv_meta={
            "c1": {"session_mode": "per_person", "person_id": "alex", "display_name": "Alex"}
        },
    )
    reflector, _, _ = _reflector(tmp_path, repo)
    buckets, _ = await reflector._collect(DAY)

    rendered = buckets["alex"].render()
    assert LONG.strip()[:40] in rendered
    assert "Saved that as a skill." in rendered


async def test_a_short_day_reports_why_it_was_skipped(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=[row("c1", "in", "hi", T10)],
        conv_meta={
            "c1": {"session_mode": "per_person", "person_id": "alex", "display_name": "Alex"}
        },
    )
    reflector, _, _ = _reflector(tmp_path, repo)

    summary = await reflector.reflect(target_date=DAY, person="alex")

    assert summary["people"] == 1
    assert summary["people_reflected"] == 0
    assert summary["posts_written"] == 0


@pytest.mark.skipif(_GIT_MISSING, reason="git binary required")
async def test_nightly_commits_the_day_page(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    wiki = layout.wiki_root(tmp_path)
    wikigit.ensure_repo(wiki)
    wikigit.commit(wiki, None, "start: sync the wiki")
    # An edit made outside Joshua during the day; the day-page commit only
    # stages what the reflection wrote, so this is left for the broad sync.
    (wiki / "note-by-a-person.md").write_text("hi\n")

    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=[row("c1", "in", LONG, T10), row("c1", "out", "Congratulations!", T11)],
        conv_meta={
            "c1": {"session_mode": "per_person", "person_id": "alex", "display_name": "Alex"}
        },
    )
    reflector, _, _ = _reflector(tmp_path, repo)

    await reflector.reflect(target_date=DAY)

    log = subprocess.run(  # noqa: ASYNC221 — test assertion, not app code
        ["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True, check=True
    ).stdout
    assert f"nightly: {DAY.isoformat()}" in log
    assert "nightly: sync the wiki" in log


async def test_nightly_writes_nothing_to_git_when_wiki_git_is_off(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=[row("c1", "in", LONG, T10), row("c1", "out", "Congratulations!", T11)],
        conv_meta={
            "c1": {"session_mode": "per_person", "person_id": "alex", "display_name": "Alex"}
        },
    )
    calls: list[tuple] = []
    import joshua_core.memory.nightly as nightly_module

    orig_commit = nightly_module.wikigit.commit

    def spy(*args, **kwargs):
        calls.append(args)
        return orig_commit(*args, **kwargs)

    reflector, _, _ = _reflector(tmp_path, repo, extra="wiki:\n  git: false\n")

    await reflector.reflect(target_date=DAY)

    assert calls == []
    assert not (layout.wiki_root(tmp_path) / ".git").exists()


async def test_journal_status_counts_today(tmp_path: Path) -> None:
    """Entries the agent wrote today, and whether the nightly page landed. The
    day page is not counted as an entry."""
    _bootstrap(tmp_path)
    reflector, _, _ = _reflector(tmp_path, FakeReflectRepo(people=PEOPLE, rows=[], conv_meta={}))
    today = date.fromisoformat(reflector.journal_status()["date"])
    day_dir = layout.journal_day_dir(today, tmp_path)
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "garden.md").write_text("beans\n")
    (day_dir / "visit.md").write_text("sister\n")
    layout.journal_day_page(today, tmp_path).write_text("the day\n")

    status = reflector.journal_status()
    assert status["entries_today"] == 2
    assert status["day_page_today"] is True
    assert status["nightly_at"] == "03:30"


async def test_journal_status_last_run_is_none_before_a_run(tmp_path: Path) -> None:
    """A restarted core reports None until the next nightly, so the day folder
    is the ground truth in the meantime."""
    _bootstrap(tmp_path)
    reflector, _, _ = _reflector(tmp_path, FakeReflectRepo(people=PEOPLE, rows=[], conv_meta={}))
    assert reflector.journal_status()["last_run"] is None


async def test_journal_status_keeps_the_last_run(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    repo = FakeReflectRepo(
        people=PEOPLE,
        rows=[row("c1", "in", LONG, T10), row("c1", "out", "Congratulations!", T11)],
        conv_meta={
            "c1": {"session_mode": "per_person", "person_id": "alex", "display_name": "Alex"}
        },
    )
    reflector, _, _ = _reflector(tmp_path, repo)
    await reflector.reflect(target_date=DAY)
    last = reflector.journal_status()["last_run"]
    assert last["date"] == DAY.isoformat()
    assert last["posts_written"] == 1
