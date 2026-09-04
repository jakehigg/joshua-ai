"""Tests for the `/data` volume layout module.

A tmp `/data` comes from the `JOSHUA_DATA_DIR` env override. Production is the
fixed `/data`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from joshua_shared import layout


@pytest.fixture
def data_dir(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "data"
    monkeypatch.setenv(layout.DATA_DIR_ENV, str(root))
    return root


def test_data_root_honors_env_override(data_dir: Path) -> None:
    assert layout.data_root() == data_dir


def test_path_helpers(data_dir: Path) -> None:
    assert layout.person_root("alex") == data_dir / "people" / "alex"
    assert layout.person_dir("alex", "attachments") == data_dir / "people" / "alex" / "attachments"
    assert layout.legacy_blog_dir("alex") == data_dir / "people" / "alex" / "blog"
    assert layout.profile_path("alex") == data_dir / "wiki" / "people" / "alex.md"
    assert layout.shared_root() == data_dir / "shared"
    assert layout.inbox_root() == data_dir / "inbox"
    assert layout.shared_profile_path() == data_dir / "wiki" / "people" / "everyone.md"
    assert layout.home_path() == data_dir / "wiki" / "Home.md"
    assert layout.people_pages_root() == data_dir / "wiki" / "people"
    assert layout.journal_root() == data_dir / "wiki" / "journal"


def test_person_dir_rejects_unknown_kind(data_dir: Path) -> None:
    with pytest.raises(ValueError):
        layout.person_dir("alex", "profile")


def test_person_dir_rejects_blog(data_dir: Path) -> None:
    """The journal lives in the wiki now; `blog` is not a `person_dir` kind."""
    with pytest.raises(ValueError):
        layout.person_dir("alex", "blog")


@pytest.mark.parametrize("bad", ["..", "a/b", "/abs", "Alex", "a" * 33, ""])
def test_safe_segment_rejects_bad_ids(bad: str) -> None:
    with pytest.raises(ValueError):
        layout.safe_segment(bad)


def test_profile_path_rejects_the_reserved_shared_id(data_dir: Path) -> None:
    with pytest.raises(ValueError):
        layout.profile_path(layout.SHARED_PROFILE_NAME)


@pytest.mark.parametrize(
    "rel",
    [
        ".git/config",
        ".trash/2026/x.md",
        "plants/.obsidian/workspace.md",
        "a/b/.hidden/c.md",
    ],
)
def test_is_hidden_true_for_a_dot_part_at_any_depth(tmp_path: Path, rel: str) -> None:
    base = tmp_path / "wiki"
    assert layout.is_hidden(base / rel, base) is True


@pytest.mark.parametrize("rel", ["plants/a.md", "a/b/c.md", "note.md"])
def test_is_hidden_false_for_a_plain_path(tmp_path: Path, rel: str) -> None:
    base = tmp_path / "wiki"
    assert layout.is_hidden(base / rel, base) is False


def test_is_hidden_ignores_a_dot_above_the_base(tmp_path: Path) -> None:
    """A dot in a parent directory, such as a host temp path, never counts."""
    base = tmp_path / ".private" / "wiki"
    assert layout.is_hidden(base / "plants" / "a.md", base) is False
    assert layout.is_hidden(base, base) is False


def test_is_hidden_false_for_a_path_outside_base(tmp_path: Path) -> None:
    base = tmp_path / "wiki"
    outside = tmp_path / "other" / ".git" / "config"
    assert layout.is_hidden(outside, base) is False


# -- journal helpers ---------------------------------------------------------


def test_journal_day_dir_and_page(data_dir: Path) -> None:
    day = date(2026, 3, 4)
    assert layout.journal_day_dir(day) == data_dir / "wiki" / "journal" / "2026" / "03" / "04"
    assert (
        layout.journal_day_page(day)
        == data_dir / "wiki" / "journal" / "2026" / "03" / "04" / "2026-03-04.md"
    )


def test_journal_entry_path(data_dir: Path) -> None:
    day = date(2026, 3, 4)
    assert (
        layout.journal_entry_path(day, "planted-tomatoes")
        == data_dir / "wiki" / "journal" / "2026" / "03" / "04" / "planted-tomatoes.md"
    )


@pytest.mark.parametrize("bad", ["", "Slug", "slug!", "-slug", "a" * 65])
def test_journal_entry_path_rejects_a_bad_slug(data_dir: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        layout.journal_entry_path(date(2026, 3, 4), bad)


def test_journal_entry_path_rejects_the_day_page_name(data_dir: Path) -> None:
    day = date(2026, 3, 4)
    with pytest.raises(ValueError):
        layout.journal_entry_path(day, "2026-03-04")


def test_journal_day_from_path_parses_a_good_path(data_dir: Path) -> None:
    day = date(2026, 3, 4)
    assert layout.journal_day_from_path(layout.journal_day_page(day)) == day
    assert layout.journal_day_from_path(layout.journal_entry_path(day, "note")) == day


@pytest.mark.parametrize(
    "rel",
    [
        "legacy/alex/note.md",
        "2026/03/x.md",
        "2026/13/04/x.md",
        "not-a-year/03/04/x.md",
    ],
)
def test_journal_day_from_path_returns_none_for_a_bad_path(data_dir: Path, rel: str) -> None:
    path = layout.journal_root() / rel
    assert layout.journal_day_from_path(path) is None


def test_journal_day_from_path_returns_none_outside_the_journal(data_dir: Path) -> None:
    assert layout.journal_day_from_path(data_dir / "wiki" / "people" / "alex.md") is None


# -- bootstrap ----------------------------------------------------------------


def test_bootstrap_person_builds_tree_and_profile(data_dir: Path) -> None:
    layout.bootstrap_person("alex", "Alex")
    home = data_dir / "people" / "alex"
    assert (home / "attachments").is_dir()
    profile = layout.profile_path("alex")
    assert profile == data_dir / "wiki" / "people" / "alex.md"
    assert profile.is_file()
    assert profile.read_text().splitlines()[0] == "# Alex"


def test_bootstrap_person_is_idempotent(data_dir: Path) -> None:
    layout.bootstrap_person("alex", "Alex")
    profile = layout.profile_path("alex")
    profile.write_text("# Alex\nedited by nightly reflection\n")
    before = profile.stat().st_mtime_ns

    layout.bootstrap_person("alex", "Alex")

    # A second bootstrap keeps the edited profile; it does not overwrite it.
    assert profile.read_text() == "# Alex\nedited by nightly reflection\n"
    assert profile.stat().st_mtime_ns == before


def test_bootstrap_shared_writes_attachments_only(data_dir: Path) -> None:
    layout.bootstrap_shared()
    assert (data_dir / "shared" / "attachments").is_dir()
    assert not (data_dir / "shared" / "README.md").exists()
    assert not (data_dir / "shared" / "profile.md").exists()


def test_bootstrap_shared_profile_writes_the_page(data_dir: Path) -> None:
    layout.bootstrap_wiki()
    layout.bootstrap_shared_profile("Test House")
    profile = layout.shared_profile_path()
    assert profile == data_dir / "wiki" / "people" / "everyone.md"
    assert profile.is_file()
    assert profile.read_text().splitlines()[0] == "# Test House"


def test_bootstrap_shared_profile_is_idempotent(data_dir: Path) -> None:
    layout.bootstrap_wiki()
    layout.bootstrap_shared_profile("Test House")
    profile = layout.shared_profile_path()
    profile.write_text("# Test House\nlearned facts\n")
    before = profile.stat().st_mtime_ns

    layout.bootstrap_shared_profile("Test House")

    assert profile.read_text() == "# Test House\nlearned facts\n"
    assert profile.stat().st_mtime_ns == before


def test_two_people_fresh_volume(data_dir: Path) -> None:
    layout.bootstrap_wiki()
    layout.bootstrap_shared()
    layout.bootstrap_shared_profile("Test House")
    layout.bootstrap_person("alex", "Alex")
    layout.bootstrap_person("remy", "Remy")
    assert layout.validate_layout() == []


def test_validate_layout_reports_missing_attachments(data_dir: Path) -> None:
    layout.bootstrap_wiki()
    layout.bootstrap_shared()
    layout.bootstrap_person("alex", "Alex")
    (data_dir / "people" / "alex" / "attachments").rmdir()

    problems = layout.validate_layout()
    assert "people/alex/attachments/ missing" in problems


def test_validate_layout_reports_missing_profile_page(data_dir: Path) -> None:
    layout.bootstrap_wiki()
    layout.bootstrap_shared()
    layout.bootstrap_person("alex", "Alex")
    layout.profile_path("alex").unlink()

    problems = layout.validate_layout()
    assert "wiki/people/alex.md missing" in problems


def test_validate_layout_reports_missing_data_root(data_dir: Path) -> None:
    problems = layout.validate_layout()
    assert problems == [f"data root missing: {data_dir}"]


def test_validate_layout_reports_missing_shared(data_dir: Path) -> None:
    data_dir.mkdir(parents=True)
    problems = layout.validate_layout()
    assert "shared/ missing" in problems


def test_root_override_ignores_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(layout.DATA_DIR_ENV, str(tmp_path / "env"))
    explicit = tmp_path / "explicit"
    layout.bootstrap_person("alex", "Alex", root=explicit)
    assert (explicit / "people" / "alex" / "attachments").is_dir()
    assert not (tmp_path / "env").exists()


def test_bootstrap_docs_publishes_the_shipped_pages(tmp_path) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    (source / "quickstart.md").write_text("# Quickstart\n")
    (source / "config.md").write_text("# Config\n")
    (source / "notes.txt").write_text("not markdown")

    root = tmp_path / "data"
    count = layout.bootstrap_docs(root, source=source)

    published = layout.docs_root(root)
    assert count == 2
    assert (published / "quickstart.md").read_text() == "# Quickstart\n"
    assert (published / "config.md").read_text() == "# Config\n"
    assert not (published / "notes.txt").exists()
    assert "Joshua's documentation" in (published / "README.md").read_text()
    assert published == root / "wiki" / "joshua-docs"


def test_bootstrap_docs_replaces_an_edited_page(tmp_path) -> None:
    """Joshua owns these files, so a second boot restores the shipped text."""
    source = tmp_path / "docs"
    source.mkdir()
    (source / "quickstart.md").write_text("# Quickstart\n")
    root = tmp_path / "data"
    layout.bootstrap_docs(root, source=source)

    edited = layout.docs_root(root) / "quickstart.md"
    edited.write_text("someone typed here\n")
    layout.bootstrap_docs(root, source=source)

    assert edited.read_text() == "# Quickstart\n"


def test_bootstrap_docs_skips_a_missing_source(tmp_path) -> None:
    root = tmp_path / "data"
    assert layout.bootstrap_docs(root, source=tmp_path / "absent") == 0
    assert not layout.docs_root(root).exists()


def test_bootstrap_wiki_creates_the_tree_and_home_once(tmp_path) -> None:
    root = tmp_path / "data"
    layout.bootstrap_wiki(root)
    wiki = layout.wiki_root(root)
    assert (wiki / "skills").is_dir()
    assert (wiki / "journal").is_dir()
    assert (wiki / "people").is_dir()
    home = layout.home_path(root)
    assert home.read_text().startswith("# Home")

    home.write_text("edited\n")
    layout.bootstrap_wiki(root)
    assert home.read_text() == "edited\n"  # idempotent, keeps an edit


def test_bootstrap_wiki_deletes_a_stale_readme(tmp_path) -> None:
    root = tmp_path / "data"
    wiki = layout.wiki_root(root)
    wiki.mkdir(parents=True)
    (wiki / "README.md").write_text("# Wiki\n")

    layout.bootstrap_wiki(root)

    assert not (wiki / "README.md").exists()
    assert layout.home_path(root).is_file()


def test_validate_layout_wants_a_wiki(tmp_path) -> None:
    root = tmp_path / "data"
    layout.bootstrap_shared(root)
    assert "wiki/ missing" in layout.validate_layout(root)
    layout.bootstrap_wiki(root)
    assert "wiki/ missing" not in layout.validate_layout(root)


# -- migrate_to_one_wiki ------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _old_tree(root: Path) -> None:
    """Build a full pre-single-wiki tree: two people, a digest, an agent post,
    a profile each, a shared profile, and both READMEs."""
    _write(root / "people" / "alex" / "blog" / "2026-03-04.md", "# 2026-03-04\nnightly digest\n")
    _write(
        root / "people" / "alex" / "blog" / "2026-03-04-0930-planted-tomatoes.md",
        "planted tomatoes\n",
    )
    _write(root / "people" / "alex" / "blog" / "notes.txt", "not a dated post\n")
    _write(root / "people" / "alex" / "profile.md", "# Alex\n")
    _write(root / "people" / "remy" / "blog" / "2026-03-05.md", "# 2026-03-05\nnightly digest\n")
    _write(root / "people" / "remy" / "profile.md", "# Remy\n")
    _write(root / "shared" / "profile.md", "# Home\n")
    _write(root / "shared" / "README.md", "# Shared\n")
    _write(root / "wiki" / "README.md", "# Wiki\n")


def test_migrate_to_one_wiki_moves_the_full_old_tree(tmp_path) -> None:
    root = tmp_path / "data"
    _old_tree(root)

    counts = layout.migrate_to_one_wiki(root)

    assert counts == {
        "journal_moved": 3,
        "journal_legacy": 1,
        "profiles_moved": 2,
        "shared_profile_moved": 1,
        "skipped": 0,
        "readmes_removed": 2,
        "docs_folder_renamed": 0,
        "docs_folder_removed": 0,
    }

    digest = root / "wiki" / "journal" / "2026" / "03" / "04" / "alex.md"
    assert digest.is_file()
    assert "people: [alex]" in digest.read_text()
    assert "date: 2026-03-04" in digest.read_text()
    assert "nightly digest" in digest.read_text()
    assert not (root / "wiki" / "journal" / "2026" / "03" / "04" / "2026-03-04.md").exists()

    entry = root / "wiki" / "journal" / "2026" / "03" / "04" / "alex-planted-tomatoes.md"
    assert entry.is_file()
    assert "people: [alex]" in entry.read_text()
    assert "planted tomatoes" in entry.read_text()

    legacy = root / "wiki" / "journal" / "legacy" / "alex" / "notes.txt"
    assert legacy.read_text() == "not a dated post\n"

    digest2 = root / "wiki" / "journal" / "2026" / "03" / "05" / "remy.md"
    assert digest2.is_file()
    assert "people: [remy]" in digest2.read_text()
    assert not (root / "wiki" / "journal" / "2026" / "03" / "05" / "2026-03-05.md").exists()

    assert (root / "wiki" / "people" / "alex.md").read_text() == "# Alex\n"
    assert (root / "wiki" / "people" / "remy.md").read_text() == "# Remy\n"
    assert (root / "wiki" / "people" / "everyone.md").read_text() == "# Home\n"

    assert not (root / "people" / "alex" / "blog").exists()
    assert not (root / "people" / "remy" / "blog").exists()
    assert not (root / "people" / "alex" / "profile.md").exists()
    assert not (root / "shared" / "profile.md").exists()
    assert not (root / "shared" / "README.md").exists()
    assert not (root / "wiki" / "README.md").exists()


def test_migrate_to_one_wiki_is_idempotent(tmp_path) -> None:
    root = tmp_path / "data"
    _old_tree(root)

    layout.migrate_to_one_wiki(root)
    second = layout.migrate_to_one_wiki(root)

    assert second == {
        "journal_moved": 0,
        "journal_legacy": 0,
        "profiles_moved": 0,
        "shared_profile_moved": 0,
        "skipped": 0,
        "readmes_removed": 0,
        "docs_folder_renamed": 0,
        "docs_folder_removed": 0,
    }


def test_migrate_to_one_wiki_does_not_overwrite_an_existing_target(tmp_path) -> None:
    root = tmp_path / "data"
    _old_tree(root)
    target = root / "wiki" / "journal" / "2026" / "03" / "04" / "alex.md"
    _write(target, "already migrated by hand\n")
    _write(root / "wiki" / "people" / "alex.md", "# Alex (already migrated)\n")

    counts = layout.migrate_to_one_wiki(root)

    assert target.read_text() == "already migrated by hand\n"
    assert (root / "wiki" / "people" / "alex.md").read_text() == "# Alex (already migrated)\n"
    # the un-overwritten sources stay in place
    assert (root / "people" / "alex" / "blog" / "2026-03-04.md").is_file()
    assert (root / "people" / "alex" / "profile.md").is_file()
    assert counts["skipped"] == 2


def test_migrate_to_one_wiki_adds_missing_keys_to_existing_front_matter(tmp_path) -> None:
    root = tmp_path / "data"
    _write(
        root / "people" / "alex" / "blog" / "2026-03-04.md",
        "---\ntitle: My day\n---\ncontent\n",
    )

    layout.migrate_to_one_wiki(root)

    text = (root / "wiki" / "journal" / "2026" / "03" / "04" / "alex.md").read_text()
    assert "title: My day" in text
    assert "people: [alex]" in text
    assert "date: 2026-03-04" in text
    assert "content" in text


def test_migrate_to_one_wiki_leaves_an_untouched_volume_alone(tmp_path) -> None:
    root = tmp_path / "data"
    layout.bootstrap_wiki(root)
    layout.bootstrap_shared(root)
    layout.bootstrap_shared_profile("Test House", root)
    layout.bootstrap_person("alex", "Alex", root=root)

    counts = layout.migrate_to_one_wiki(root)

    assert counts == {
        "journal_moved": 0,
        "journal_legacy": 0,
        "profiles_moved": 0,
        "shared_profile_moved": 0,
        "skipped": 0,
        "readmes_removed": 0,
        "docs_folder_renamed": 0,
        "docs_folder_removed": 0,
    }


def test_migrate_to_one_wiki_disambiguates_a_same_slug_collision(tmp_path) -> None:
    """Two posts, one person, one day, the same slug: the second post falls
    back to its own time instead of being skipped and left behind."""
    root = tmp_path / "data"
    _write(root / "people" / "alex" / "blog" / "2026-08-29-1336-haiku.md", "first haiku\n")
    _write(root / "people" / "alex" / "blog" / "2026-08-29-1342-haiku.md", "second haiku\n")

    counts = layout.migrate_to_one_wiki(root)

    day_dir = root / "wiki" / "journal" / "2026" / "08" / "29"
    assert "first haiku" in (day_dir / "alex-haiku.md").read_text()
    assert "second haiku" in (day_dir / "alex-haiku-1342.md").read_text()
    assert counts["journal_moved"] == 2
    assert counts["skipped"] == 0

    second = layout.migrate_to_one_wiki(root)
    assert second == {
        "journal_moved": 0,
        "journal_legacy": 0,
        "profiles_moved": 0,
        "shared_profile_moved": 0,
        "skipped": 0,
        "readmes_removed": 0,
        "docs_folder_renamed": 0,
        "docs_folder_removed": 0,
    }


def test_migrate_to_one_wiki_names_an_old_digest_by_person(tmp_path) -> None:
    """Two people's digests on one day never collide: `YYYY-MM-DD.md` is
    reserved for the new instance-wide nightly page, so each old digest gets
    its own page named by the person id."""
    root = tmp_path / "data"
    _write(
        root / "people" / "alex" / "blog" / "2026-08-29.md",
        "---\nperson: alex\n---\n# 2026-08-29\nalex's day\n",
    )
    _write(root / "people" / "mia" / "blog" / "2026-08-29.md", "# 2026-08-29\nmia's day\n")

    counts = layout.migrate_to_one_wiki(root)

    day_dir = root / "wiki" / "journal" / "2026" / "08" / "29"
    alex_page = day_dir / "alex.md"
    mia_page = day_dir / "mia.md"
    assert alex_page.is_file()
    assert mia_page.is_file()
    assert not (day_dir / "2026-08-29.md").exists()

    alex_text = alex_page.read_text()
    assert "person: alex" in alex_text  # an existing key survives untouched
    assert "people: [alex]" in alex_text
    assert "date: 2026-08-29" in alex_text
    assert "people: [mia]" in mia_page.read_text()

    assert counts["journal_moved"] == 2
    assert counts["skipped"] == 0

    second = layout.migrate_to_one_wiki(root)
    assert second == {
        "journal_moved": 0,
        "journal_legacy": 0,
        "profiles_moved": 0,
        "shared_profile_moved": 0,
        "skipped": 0,
        "readmes_removed": 0,
        "docs_folder_renamed": 0,
        "docs_folder_removed": 0,
    }


def test_migrate_to_one_wiki_replaces_an_unedited_shared_profile_template(tmp_path) -> None:
    """A boot that ran the old, wrong order left `everyone.md` as the
    unedited template. A migration that finds it now must not treat it as
    the real shared profile and skip forever."""
    root = tmp_path / "data"
    layout.bootstrap_wiki(root)
    layout.bootstrap_shared_profile("Test House", root)
    _write(root / "shared" / "profile.md", "# Test House\nreal shared facts\n")

    counts = layout.migrate_to_one_wiki(root)

    assert layout.shared_profile_path(root).read_text() == "# Test House\nreal shared facts\n"
    assert not (root / "shared" / "profile.md").exists()
    assert counts["shared_profile_moved"] == 1
    assert counts["skipped"] == 0


def test_migrate_to_one_wiki_does_not_replace_an_edited_shared_profile(tmp_path) -> None:
    """A real, edited `everyone.md` is never overwritten; the source stays
    and the move counts as skipped."""
    root = tmp_path / "data"
    layout.bootstrap_wiki(root)
    layout.bootstrap_shared_profile("Test House", root)
    edited = layout.shared_profile_path(root)
    edited.write_text("# Test House\n## About\nAlex and Mia live here.\n")
    _write(root / "shared" / "profile.md", "# Test House\nsomething else\n")

    counts = layout.migrate_to_one_wiki(root)

    assert edited.read_text() == "# Test House\n## About\nAlex and Mia live here.\n"
    assert (root / "shared" / "profile.md").is_file()
    assert counts["shared_profile_moved"] == 0
    assert counts["skipped"] == 1


def test_migrate_to_one_wiki_renames_the_old_docs_folder(tmp_path) -> None:
    """`wiki/joshua/` is the old name; the migration renames it in place when
    `wiki/joshua-docs/` is not there yet."""
    root = tmp_path / "data"
    _write(root / "wiki" / "joshua" / "quickstart.md", "# Quickstart\n")

    counts = layout.migrate_to_one_wiki(root)

    assert not (root / "wiki" / "joshua").exists()
    assert (layout.docs_root(root) / "quickstart.md").read_text() == "# Quickstart\n"
    assert counts["docs_folder_renamed"] == 1
    assert counts["docs_folder_removed"] == 0


def test_migrate_to_one_wiki_removes_the_old_docs_folder_when_both_exist(tmp_path) -> None:
    """When `wiki/joshua-docs/` already exists, core has already replaced the
    docs at the new path, so the stale `wiki/joshua/` is removed outright."""
    root = tmp_path / "data"
    _write(root / "wiki" / "joshua" / "quickstart.md", "# Old quickstart\n")
    _write(root / "wiki" / "joshua-docs" / "quickstart.md", "# New quickstart\n")

    counts = layout.migrate_to_one_wiki(root)

    assert not (root / "wiki" / "joshua").exists()
    assert (layout.docs_root(root) / "quickstart.md").read_text() == "# New quickstart\n"
    assert counts["docs_folder_renamed"] == 0
    assert counts["docs_folder_removed"] == 1


def test_migrate_to_one_wiki_docs_folder_step_is_idempotent(tmp_path) -> None:
    root = tmp_path / "data"
    _write(root / "wiki" / "joshua" / "quickstart.md", "# Quickstart\n")

    first = layout.migrate_to_one_wiki(root)
    second = layout.migrate_to_one_wiki(root)

    assert first["docs_folder_renamed"] == 1
    assert second["docs_folder_renamed"] == 0
    assert second["docs_folder_removed"] == 0
    assert (layout.docs_root(root) / "quickstart.md").read_text() == "# Quickstart\n"


# -- the writability check -------------------------------------------------


def test_validate_layout_reports_no_fault_for_a_writable_root(data_dir: Path) -> None:
    """The check writes a file. It does not read the mode bits, which can lie.

    On an NFS export the server decides who may write, so a data root core had
    written to for the life of the instance read as not writable and `/readyz`
    reported a fault that was not there.
    """
    layout.bootstrap_wiki()
    layout.bootstrap_shared()
    assert [p for p in layout.validate_layout() if "not writable" in p] == []


def test_validate_layout_still_reports_a_root_it_cannot_write(tmp_path: Path) -> None:
    """A path below a regular file can never be made, whatever the uid is."""
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    problems = layout.validate_layout(blocker / "data")
    assert problems == [f"data root missing: {blocker / 'data'}"]


def test_validate_layout_leaves_no_probe_file(data_dir: Path) -> None:
    layout.bootstrap_wiki()
    layout.bootstrap_shared()
    layout.validate_layout()
    assert [p.name for p in data_dir.iterdir() if p.name.startswith(".joshua-write-probe")] == []
