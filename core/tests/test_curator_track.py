"""The curator tracking keys: the body hash, the skip rule, and where the keys
may go."""

from __future__ import annotations

from datetime import date

import pytest
from joshua_core.jobs.curator import track
from joshua_shared import frontmatter

DAY = date(2026, 10, 19)


def test_body_hash_ignores_the_frontmatter_and_trailing_space() -> None:
    plain = "# Roses\n\nPrune in March.\n"
    assert track.body_hash(plain) == track.body_hash("---\na: 1\n---\n" + plain + "\n\n  ")
    assert len(track.body_hash(plain)) == track.HASH_CHARS
    assert track.body_hash(plain) != track.body_hash("# Roses\n\nPrune in April.\n")


def test_needs_review_and_with_tracking() -> None:
    text = "---\ntitle: Roses\n---\n# Roses\n"
    assert track.needs_review(text)
    tracked = track.with_tracking(text, DAY)
    assert not track.needs_review(tracked)
    assert frontmatter.split(tracked)[1] == frontmatter.split(text)[1]
    meta, _ = frontmatter.split(tracked)
    assert meta == {"title": "Roses", "curated": DAY, "curated_hash": track.body_hash(text)}
    assert track.curated_day(tracked) == DAY
    edited = tracked + "More.\n"
    assert track.needs_review(edited)
    assert not track.needs_review(frontmatter.update(tracked, tags=["x"]))


def test_the_two_pairs_are_checked_on_their_own() -> None:
    text = "---\ntitle: Roses\n---\n# Roses\n"
    assert track.needs_place(text) and track.needs_refresh(text)
    placed = track.with_tracking(text, DAY)
    assert not track.needs_place(placed) and track.needs_refresh(placed)
    assert track.refreshed_day(placed) is None
    both = track.with_tracking(placed, DAY, curated=False, refreshed=True)
    assert not track.needs_place(both) and not track.needs_refresh(both)
    assert track.refreshed_day(both) == DAY
    meta, _ = frontmatter.split(both)
    assert meta["refreshed_hash"] == meta["curated_hash"] == track.body_hash(text)
    edited = both + "More.\n"
    assert track.needs_place(edited) and track.needs_refresh(edited)
    assert track.with_tracking(text, DAY, curated=False) == text
    assert track.needs_review(placed) is track.needs_place(placed)


def test_a_page_with_no_frontmatter_keeps_its_bytes_as_the_body() -> None:
    text = "Watered the plants.\n"
    tracked = track.with_tracking(text, DAY)
    assert tracked.endswith(text)
    assert frontmatter.split(tracked)[1] == text


def test_curated_day_reads_a_string_and_refuses_junk() -> None:
    assert track.curated_day("---\ncurated: '2026-10-01'\n---\n") == date(2026, 10, 1)
    assert track.curated_day("---\ncurated: soon\n---\n") is None
    assert track.curated_day("---\ncurated: 3\n---\n") is None
    assert track.curated_day("no keys") is None


@pytest.mark.parametrize(
    ("rel", "text", "ok"),
    [
        ("garden/roses.md", "# Roses\n", True),
        ("skills/water.md", "# Skill\n", True),
        ("journal/2026/10/01/a.md", "Entry\n", True),
        ("journal/2026/10/01/r.md", "---\nrun_id: 4\n---\nReport\n", False),
        ("people/alex.md", "# Alex\n", False),
        ("joshua-docs/jobs.md", "# Jobs\n", False),
        (".trash/x.md", "x\n", False),
        ("garden/.hidden.md", "x\n", False),
        ("garden/photo.jpg", "x", False),
        ("", "x", False),
    ],
)
def test_may_track(rel: str, text: str, ok: bool) -> None:
    assert track.may_track(rel, text) is ok
