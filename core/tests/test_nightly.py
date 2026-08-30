"""Offline tests for the nightly reflection (canned oneshot, no DB, no SDK)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from joshua_core.memory.nightly import NightlyReflector
from joshua_core.store.models import Person
from joshua_shared import layout
from nightly_fakes import (
    FakeIndexer,
    FakeManager,
    FakeReflectRepo,
    canned_oneshot,
    junk_oneshot,
    make_settings,
    row,
)

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
    layout.bootstrap_shared("Test House", root)
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


def _blog(root: Path, pid: str, d: date = DAY) -> Path:
    return layout.person_dir(pid, "blog", root) / f"{d.isoformat()}.md"


async def test_writes_post_and_profile(tmp_path: Path) -> None:
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

    post = _blog(tmp_path, "alex")
    assert post.exists()
    text = post.read_text()
    assert text.startswith("---\ndate: 2026-08-26\nperson: alex\nsource: nightly\n---\n")
    assert "Alex had a good day." in text
    assert "Likes tea." in layout.profile_path("alex", tmp_path).read_text()
    assert {"source": "files", "person": "alex"} in indexer.calls
    assert summary["posts_written"] == 1
    assert summary["profiles_updated"] == 1


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

    assert not _blog(tmp_path, "alex").exists()
    assert layout.profile_path("alex", tmp_path).read_text() == before
    assert indexer.calls == []
    assert summary["posts_written"] == 0


async def test_rerun_overwrites_the_post(tmp_path: Path) -> None:
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

    posts = list(layout.person_dir("alex", "blog", tmp_path).glob("2026-08-26*.md"))
    assert len(posts) == 1
    assert "A different second entry." in posts[0].read_text()


async def test_guest_dm_gets_a_post(tmp_path: Path) -> None:
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

    assert _blog(tmp_path, "gwen").exists()


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

    assert _blog(tmp_path, "alex").exists()
    assert not _blog(tmp_path, "gwen").exists()
    # The shared pass ran over the group chat.
    assert summary["shared_updated"] is True
    assert "Quiet weeknights." in layout.shared_profile_path(tmp_path).read_text()
    assert {"source": "files", "person": None} in indexer.calls


async def test_unparseable_reflection_skips_person(tmp_path: Path) -> None:
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

    assert not _blog(tmp_path, "alex").exists()
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

    assert not _blog(tmp_path, "alex").exists()


async def test_a_journaled_day_still_reflects(tmp_path: Path) -> None:
    """A regression: the agent journals with `write_file` during an
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
                tools=["mcp__files__write_file"],
                written=["blog/moving-to-boston.md"],
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
