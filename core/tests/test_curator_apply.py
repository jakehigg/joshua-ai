"""The curator apply step: each action, the checks it repeats on the disk, and
the writes it refuses."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from joshua_core.jobs.curator.apply import (
    ApplyRefused,
    Workspace,
    add_index_line,
    apply_patch,
    finds_once,
)
from joshua_core.jobs.curator.track import (
    needs_place,
    needs_refresh,
    needs_review,
    with_tracking,
)
from joshua_shared import frontmatter

DAY = date(2026, 10, 19)


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    wiki = tmp_path / "data" / "wiki"
    pages = {
        "Home.md": "# Home\n\n- [Garden](garden.md)\n",
        "garden.md": (
            "# Garden\n\n- [Roses](garden/roses.md)\n\n## Pages\n\n- [A](garden/a.md)\n\n## End\n"
        ),
        "garden/roses.md": "# Roses\n\nPrune in March.\n",
        "kitchen.md": "# Kitchen\n",
        "kitchen/bread.md": "# Bread\n\nSee [roses](../garden/roses.md).\n",
        "journal/2026/10/01/a.md": "An entry about [roses](../../../../garden/roses.md).\n",
        "people/alex.md": "# Alex\n",
        "skills/s.md": "---\nname: s\n---\n# Skill\n",
    }
    for rel, text in pages.items():
        path = wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return wiki


def _ws(wiki: Path) -> Workspace:
    return Workspace(wiki, DAY)


def test_write_refuses_people_docs_and_dot_paths(wiki: Path) -> None:
    ws = _ws(wiki)
    for rel in ("people/alex.md", "joshua-docs/x.md", ".trash/x.md", "garden/.x.md"):
        with pytest.raises(ApplyRefused):
            ws.write(rel, "changed\n")
    assert (wiki / "people/alex.md").read_text() == "# Alex\n"


def test_write_refuses_a_journal_body_and_a_skill_frontmatter(wiki: Path) -> None:
    ws = _ws(wiki)
    with pytest.raises(ApplyRefused):
        ws.write("journal/2026/10/01/a.md", "Another body.\n")
    with pytest.raises(ApplyRefused):
        ws.write("skills/s.md", "---\nname: t\n---\n# Skill\n")
    assert ws.track("journal/2026/10/01/a.md")
    assert ws.track("skills/s.md")
    assert not ws.track("skills/s.md")
    assert not ws.track("people/alex.md")
    assert not ws.track("nowhere.md")


def test_move_and_trash_refuse_what_they_must(wiki: Path) -> None:
    ws = _ws(wiki)
    with pytest.raises(ApplyRefused):
        ws.move("journal/2026/10/01/a.md", "garden/a.md")
    with pytest.raises(ApplyRefused):
        ws.move("people/alex.md", "garden/alex.md")
    with pytest.raises(ApplyRefused):
        ws.move("garden/nope.md", "kitchen/nope.md")
    with pytest.raises(ApplyRefused):
        ws.move("garden/roses.md", "kitchen/bread.md")
    with pytest.raises(ApplyRefused):
        ws.move("garden/roses.md", "attic/roses.md")
    for rel in ("journal/2026/10/01/a.md", "people/alex.md", ".git/x", "nope.md"):
        with pytest.raises(ApplyRefused):
            ws.trash(rel)
    ws.trash("kitchen.md")
    (wiki / "kitchen.md").write_text("again\n")
    with pytest.raises(ApplyRefused):
        ws.trash("kitchen.md")


def test_relink_and_drop_line_check_the_disk(wiki: Path) -> None:
    ws = _ws(wiki)
    apply_patch(
        ws,
        {
            "action": "relink",
            "path": "garden.md",
            "old": "[Roses](garden/roses.md)",
            "new": "[Roses](/wiki/garden/roses.md)",
        },
    )
    assert "[Roses](/wiki/garden/roses.md)" in (wiki / "garden.md").read_text()
    with pytest.raises(ApplyRefused):
        apply_patch(ws, {"action": "relink", "path": "garden.md", "old": "[x](y)", "new": "z"})
    apply_patch(ws, {"action": "drop-line", "path": "garden.md", "line": "- [A](garden/a.md)"})
    assert "[A]" not in (wiki / "garden.md").read_text()
    with pytest.raises(ApplyRefused):
        apply_patch(ws, {"action": "drop-line", "path": "garden.md", "line": "- [A](garden/a.md)"})
    assert ws.written == ["garden.md"]


def test_index_add_and_set_date(wiki: Path) -> None:
    ws = _ws(wiki)
    patch = {
        "action": "index-add",
        "path": "kitchen.md",
        "page": "kitchen/bread.md",
        "line": "- [Bread](/wiki/kitchen/bread.md)",
    }
    apply_patch(ws, patch)
    apply_patch(ws, patch)
    assert (wiki / "kitchen.md").read_text() == (
        "# Kitchen\n\n## Pages\n\n- [Bread](/wiki/kitchen/bread.md)\n"
    )
    with pytest.raises(ApplyRefused):
        apply_patch(ws, {**patch, "page": "kitchen/none.md"})
    with pytest.raises(ApplyRefused):
        apply_patch(ws, {**patch, "path": "none.md"})
    apply_patch(ws, {"action": "set-date", "path": "journal/2026/10/01/a.md", "date": "2026-10-01"})
    text = (wiki / "journal/2026/10/01/a.md").read_text()
    assert text.startswith("---\ndate: 2026-10-01\n---\nAn entry")
    with pytest.raises(ApplyRefused):
        apply_patch(
            ws, {"action": "set-date", "path": "journal/2026/10/01/a.md", "date": "2026-10-01"}
        )


def test_add_index_line_places_the_line() -> None:
    assert add_index_line("", "- [A](a.md)") == "## Pages\n\n- [A](a.md)\n"
    text = "# X\n\n## Pages\n\n- [A](a.md)\n\n## End\n\nmore\n"
    assert add_index_line(text, "- [B](b.md)") == (
        "# X\n\n## Pages\n\n- [A](a.md)\n- [B](b.md)\n\n## End\n\nmore\n"
    )
    assert add_index_line("# X\n\n## Pages\n", "- [B](b.md)") == "# X\n\n## Pages\n\n- [B](b.md)\n"
    assert add_index_line("# X\r\n", "- [B](b.md)") == "# X\r\n\r\n## Pages\r\n\r\n- [B](b.md)\r\n"


def test_move_rewrites_links_but_not_the_journal(wiki: Path) -> None:
    roses = wiki / "garden/roses.md"
    roses.write_text(with_tracking(roses.read_text() + "[[bread]]\n", DAY))
    journal = (wiki / "journal/2026/10/01/a.md").read_bytes()
    ws = _ws(wiki)
    apply_patch(ws, {"action": "move", "path": "garden/roses.md", "to": "kitchen/roses.md"})
    assert not roses.exists()
    assert "[roses](roses.md)" in (wiki / "kitchen/bread.md").read_text()
    assert "garden/roses.md" not in (wiki / "garden.md").read_text()
    assert "- [Roses](/wiki/kitchen/roses.md)" in (wiki / "kitchen.md").read_text()
    assert (wiki / "journal/2026/10/01/a.md").read_bytes() == journal
    assert "kitchen/roses.md" in ws.clean
    assert ws.current("garden/roses.md") == "kitchen/roses.md"
    for rel in sorted(ws.clean):
        ws.track(rel)
    assert not needs_review((wiki / "kitchen/roses.md").read_text())
    with pytest.raises(ApplyRefused):
        apply_patch(ws, {"action": "move", "path": "kitchen/bread.md", "to": "people/bread.md"})
    with pytest.raises(ApplyRefused):
        apply_patch(ws, {"action": "move", "path": "garden/roses.md", "to": "garden/x.md"})


def test_merge_points_links_at_the_kept_page(wiki: Path) -> None:
    (wiki / "garden/rose-care.md").write_text("# Rose care\n")
    garden = wiki / "garden.md"
    garden.write_text(garden.read_text() + "- [Care](garden/rose-care.md)\n")
    bread = wiki / "kitchen/bread.md"
    bread.write_text(bread.read_text() + "And [care](../garden/rose-care.md).\n")
    ws = _ws(wiki)
    apply_patch(ws, {"action": "merge", "path": "garden/rose-care.md", "into": "garden/roses.md"})
    assert not (wiki / "garden/rose-care.md").exists()
    assert (wiki / ".trash/2026-10-19/garden/rose-care.md").is_file()
    assert "rose-care" not in garden.read_text()
    assert "[care](../garden/roses.md)" in bread.read_text()
    assert ".trash/2026-10-19/garden/rose-care.md" in ws.written
    assert ws.current("garden/rose-care.md") is None
    for patch in (
        {"action": "merge", "path": "garden/none.md", "into": "garden/roses.md"},
        {"action": "merge", "path": "garden/roses.md", "into": "garden/none.md"},
        {"action": "merge", "path": "garden/roses.md", "into": "garden/roses.md"},
        {"action": "merge", "path": "journal/2026/10/01/a.md", "into": "garden/roses.md"},
    ):
        with pytest.raises(ApplyRefused):
            apply_patch(ws, patch)


def test_refresh_applies_in_order_or_not_at_all(wiki: Path) -> None:
    ws = _ws(wiki)
    rel = "garden/roses.md"
    before = (wiki / rel).read_text()
    bad = [{"find": "March", "replace": "April"}, {"find": "nothing", "replace": "x"}]
    with pytest.raises(ApplyRefused):
        apply_patch(ws, {"action": "refresh", "path": rel, "patch": bad})
    assert (wiki / rel).read_text() == before
    assert not finds_once(before, bad)
    good = [{"find": "March", "replace": "April"}, {"find": "April", "replace": "May"}]
    assert finds_once(before, good)
    apply_patch(ws, {"action": "refresh", "path": rel, "patch": good})
    assert "Prune in May." in (wiki / rel).read_text()
    for patch in (
        {"action": "refresh", "path": rel, "patch": []},
        {"action": "refresh", "path": "none.md", "patch": good},
        {"action": "unknown"},
        {"action": "relink", "path": rel},
    ):
        with pytest.raises(ApplyRefused):
            apply_patch(ws, patch)


def test_both_pairs_follow_a_move_and_come_back_after_the_change(wiki: Path) -> None:
    roses = wiki / "garden/roses.md"
    roses.write_text(with_tracking(roses.read_text(), DAY, curated=True, refreshed=True))
    ws = _ws(wiki)
    apply_patch(ws, {"action": "move", "path": "garden/roses.md", "to": "kitchen/roses.md"})
    assert "kitchen/roses.md" in ws.clean and "kitchen/roses.md" in ws.refreshed_clean
    ws.track_clean()
    moved = (wiki / "kitchen/roses.md").read_text()
    assert not needs_place(moved) and not needs_refresh(moved)


def test_track_writes_only_the_pairs_asked_for(wiki: Path) -> None:
    ws = _ws(wiki)
    assert ws.track("garden/roses.md", curated=False, refreshed=True)
    text = (wiki / "garden/roses.md").read_text()
    assert needs_place(text) and not needs_refresh(text)
    assert not ws.track("garden/roses.md", curated=False, refreshed=True)
    assert not ws.track("garden/roses.md", curated=False, refreshed=False)
    assert ws.track("garden/roses.md")
    assert not needs_place((wiki / "garden/roses.md").read_text())


def test_a_skill_takes_the_refreshed_pair_and_nothing_else(wiki: Path) -> None:
    ws = _ws(wiki)
    assert ws.track("skills/s.md", refreshed=True)
    meta = frontmatter.split((wiki / "skills/s.md").read_text())[0]
    assert {"refreshed", "refreshed_hash", "curated", "curated_hash"} <= set(meta)
    with pytest.raises(ApplyRefused):
        ws.write(
            "skills/s.md", (wiki / "skills/s.md").read_text().replace("---\n", "---\nx: 1\n", 1)
        )


def test_a_trashed_page_leaves_both_clean_sets(wiki: Path) -> None:
    roses = wiki / "garden/roses.md"
    roses.write_text(with_tracking(roses.read_text(), DAY, refreshed=True))
    ws = _ws(wiki)
    ws.write("garden/roses.md", roses.read_text() + "More.\n")
    assert "garden/roses.md" in ws.refreshed_clean
    ws.trash("garden/roses.md")
    assert "garden/roses.md" not in ws.clean | ws.refreshed_clean
