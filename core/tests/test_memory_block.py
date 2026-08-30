"""Unit tests for the memory recency tier.

Offline: builds the block from files under a temp data dir and drives the
manager's session rebuild with the stub backend. No DB, no SDK.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from engine_fakes import (
    FakeRepo,
    fake_derive_profile,
    make_channel,
    make_conversation,
    make_settings,
)
from joshua_core.engine.manager import ConversationManager
from joshua_core.memory.prompt import build_memory_block, render
from joshua_core.store.models import Person
from joshua_shared import layout

_MEMBER = Person(id="alex", display_name="Alex", role="member")
_GUEST = Person(id="sam", display_name="Sam", role="guest")
_DM = make_channel(session_mode="per_person")
_SHARED = make_channel(channel_id="telegram:g", session_mode="shared")


def _seed_person(root: Path, person: Person, profile: str = "") -> None:
    layout.bootstrap_person(person.id, person.display_name, root=root)
    if profile:
        layout.profile_path(person.id, root).write_text(profile)


def _write_post(root: Path, pid: str, day: str, body: str) -> None:
    (layout.person_dir(pid, "blog", root) / f"{day}.md").write_text(body)


# --- build_memory_block ----------------------------------------------------


def test_recent_posts_are_the_n_newest_dates(tmp_path: Path) -> None:
    _seed_person(tmp_path, _MEMBER)
    for day in ("2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23", "2026-08-24"):
        _write_post(tmp_path, "alex", day, f"post for {day}")

    block = build_memory_block(_MEMBER, _DM, recent_posts=3, data_dir=tmp_path)

    assert block is not None
    dates = [d for d, _ in block.recent_posts]
    assert dates == [date(2026, 8, 24), date(2026, 8, 23), date(2026, 8, 22)]


def test_recent_posts_truncates_the_oldest_first(tmp_path: Path) -> None:
    _seed_person(tmp_path, _MEMBER)
    _write_post(tmp_path, "alex", "2026-08-24", "N" * 40)
    _write_post(tmp_path, "alex", "2026-08-23", "M" * 40)
    _write_post(tmp_path, "alex", "2026-08-22", "O" * 40)

    # Budget holds the two newest whole and cuts into the oldest.
    block = build_memory_block(_MEMBER, _DM, recent_posts=3, recent_max_chars=90, data_dir=tmp_path)

    assert block is not None
    newest, middle, oldest = block.recent_posts
    assert newest[1] == "N" * 40  # newest untouched
    assert middle[1] == "M" * 40
    assert oldest[0] == date(2026, 8, 22)
    assert len(oldest[1]) == 10  # 90 - 40 - 40
    assert set(oldest[1]) == {"O"}


def test_group_session_gets_only_the_shared_profile(tmp_path: Path) -> None:
    layout.bootstrap_shared(root=tmp_path)
    layout.shared_profile_path(tmp_path).write_text("# Home\n## About\nWe are four.")

    block = build_memory_block(None, _SHARED, data_dir=tmp_path)

    assert block is not None
    assert block.profile_md == ""
    assert block.recent_posts == []
    assert block.shared_md is not None and "We are four." in block.shared_md
    rendered = render(block)
    assert "## Shared profile" in rendered
    assert "## Recent days" not in rendered


def test_group_session_without_shared_profile_is_none(tmp_path: Path) -> None:
    assert build_memory_block(None, _SHARED, data_dir=tmp_path) is None


def test_person_none_on_per_person_is_none(tmp_path: Path) -> None:
    assert build_memory_block(None, _DM, data_dir=tmp_path) is None


def test_member_includes_shared_section(tmp_path: Path) -> None:
    _seed_person(tmp_path, _MEMBER)
    layout.bootstrap_shared(root=tmp_path)
    layout.shared_profile_path(tmp_path).write_text("# Home\nShared facts.")

    block = build_memory_block(_MEMBER, _DM, data_dir=tmp_path)

    assert block is not None
    assert block.shared_md is not None and "Shared facts." in block.shared_md


def test_guest_has_no_shared_section(tmp_path: Path) -> None:
    _seed_person(tmp_path, _GUEST)
    layout.bootstrap_shared(root=tmp_path)
    layout.shared_profile_path(tmp_path).write_text("# Home\nShared facts.")

    block = build_memory_block(_GUEST, _DM, data_dir=tmp_path)

    assert block is not None
    assert block.shared_md is None


def test_shared_profile_capped_at_max_chars(tmp_path: Path) -> None:
    _seed_person(tmp_path, _MEMBER)
    layout.bootstrap_shared(root=tmp_path)
    layout.shared_profile_path(tmp_path).write_text("# Home\n" + "x" * 5000)

    block = build_memory_block(_MEMBER, _DM, shared_max_chars=100, data_dir=tmp_path)

    assert block is not None
    assert block.shared_md is not None and len(block.shared_md) == 100


def test_profile_drops_title_and_template_comment(tmp_path: Path) -> None:
    _seed_person(
        tmp_path,
        _MEMBER,
        profile="# Alex\n<!-- rewritten nightly -->\n## About\nLikes tea.",
    )

    block = build_memory_block(_MEMBER, _DM, data_dir=tmp_path)

    assert block is not None
    assert block.profile_md == "## About\nLikes tea."
    rendered = render(block)
    assert "## About Alex (your profile of them — maintained nightly)" in rendered
    assert "# Alex\n" not in rendered
    assert "rewritten nightly" not in rendered


def test_render_orders_profile_then_days_then_shared(tmp_path: Path) -> None:
    _seed_person(tmp_path, _MEMBER, profile="# Alex\n## About\nA person.")
    _write_post(tmp_path, "alex", "2026-08-24", "Today happened.")
    layout.bootstrap_shared(root=tmp_path)
    layout.shared_profile_path(tmp_path).write_text("# Home\nShared facts.")

    rendered = render(build_memory_block(_MEMBER, _DM, data_dir=tmp_path))

    profile_at = rendered.index("## About Alex")
    days_at = rendered.index("## Recent days")
    day_at = rendered.index("### 2026-08-24")
    shared_at = rendered.index("## Shared profile")
    assert profile_at < days_at < day_at < shared_at


# --- manager rebuild on a newer profile ------------------------------------


class SpyComposer:
    def __init__(self) -> None:
        self.calls = 0

    def compose(self, profile, person, channel, memory=None) -> str:
        self.calls += 1
        return "you are joshua"


async def test_pooled_session_rebuilds_when_profile_changes(tmp_path: Path) -> None:
    _seed_person(tmp_path, _MEMBER, profile="# Alex\n## About\nOld.")
    composer = SpyComposer()
    manager = ConversationManager(
        repo=FakeRepo(),
        settings=make_settings(),
        composer=composer,
        derive_profile=fake_derive_profile,
        agent_backend="stub",
        data_dir=tmp_path,
    )
    channel, conversation = make_channel(), make_conversation(person_id="alex")

    first = await manager._get_or_create(channel, conversation)
    assert composer.calls == 1

    # A same-state second call reuses the session; the composer is not re-run.
    await manager._get_or_create(channel, conversation)
    assert composer.calls == 1

    # The nightly rewrite bumps profile.md mtime -> the next turn rebuilds.
    profile = layout.profile_path("alex", tmp_path)
    profile.write_text("# Alex\n## About\nNew.")
    os.utime(profile, (first.memory_mtime + 100, first.memory_mtime + 100))

    await manager._get_or_create(channel, conversation)
    assert composer.calls == 2
