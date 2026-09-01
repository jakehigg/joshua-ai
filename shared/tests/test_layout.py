"""Tests for the `/data` volume layout module.

A tmp `/data` comes from the `JOSHUA_DATA_DIR` env override. Production is the
fixed `/data`.
"""

from __future__ import annotations

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
    assert layout.person_dir("alex", "blog") == data_dir / "people" / "alex" / "blog"
    assert layout.profile_path("alex") == data_dir / "people" / "alex" / "profile.md"
    assert layout.shared_root() == data_dir / "shared"
    assert layout.inbox_root() == data_dir / "inbox"
    assert layout.shared_profile_path() == data_dir / "shared" / "profile.md"


def test_person_dir_rejects_unknown_kind(data_dir: Path) -> None:
    with pytest.raises(ValueError):
        layout.person_dir("alex", "profile")


@pytest.mark.parametrize("bad", ["..", "a/b", "/abs", "Alex", "a" * 33, ""])
def test_safe_segment_rejects_bad_ids(bad: str) -> None:
    with pytest.raises(ValueError):
        layout.safe_segment(bad)


def test_bootstrap_person_builds_tree_and_profile(data_dir: Path) -> None:
    layout.bootstrap_person("alex", "Alex")
    home = data_dir / "people" / "alex"
    for sub in ("blog", "attachments"):
        assert (home / sub).is_dir()
    profile = home / "profile.md"
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


def test_bootstrap_shared_writes_readme_and_profile(data_dir: Path) -> None:
    layout.bootstrap_shared("Test House")
    assert (data_dir / "shared" / "README.md").is_file()
    profile = layout.shared_profile_path()
    assert profile.is_file()
    assert profile.read_text().splitlines()[0] == "# Test House"


def test_bootstrap_shared_is_idempotent(data_dir: Path) -> None:
    layout.bootstrap_shared("Test House")
    profile = layout.shared_profile_path()
    profile.write_text("# Test House\nlearned facts\n")
    before = profile.stat().st_mtime_ns

    layout.bootstrap_shared("Test House")

    assert profile.read_text() == "# Test House\nlearned facts\n"
    assert profile.stat().st_mtime_ns == before


def test_two_people_fresh_volume(data_dir: Path) -> None:
    layout.bootstrap_wiki()
    layout.bootstrap_shared("Test House")
    layout.bootstrap_person("alex", "Alex")
    layout.bootstrap_person("remy", "Remy")
    assert layout.validate_layout() == []


def test_validate_layout_reports_missing_blog(data_dir: Path) -> None:
    layout.bootstrap_shared("Test House")
    layout.bootstrap_person("alex", "Alex")
    (data_dir / "people" / "alex" / "blog").rmdir()

    problems = layout.validate_layout()
    assert "people/alex/blog/ missing" in problems


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
    assert (explicit / "people" / "alex" / "blog").is_dir()
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
    assert published == root / "wiki" / "joshua"


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


def test_bootstrap_wiki_creates_the_tree(tmp_path) -> None:
    root = tmp_path / "data"
    assert layout.bootstrap_wiki(root) == 0
    wiki = layout.wiki_root(root)
    assert (wiki / "skills").is_dir()
    assert (wiki / "README.md").read_text().startswith("# Wiki")
    readme = wiki / "README.md"
    readme.write_text("edited\n")
    layout.bootstrap_wiki(root)
    assert readme.read_text() == "edited\n"  # idempotent, keeps an edit


def test_bootstrap_wiki_moves_legacy_per_person_pages(tmp_path) -> None:
    root = tmp_path / "data"
    legacy = root / "people" / "alex" / "wiki"
    (legacy / "recipes").mkdir(parents=True)
    (legacy / "recipes" / "pizza.md").write_text("# Pizza\n")
    (legacy / "note.md").write_text("note\n")
    (root / "wiki" / "alex").mkdir(parents=True)
    (root / "wiki" / "alex" / "note.md").write_text("already here\n")

    moved = layout.bootstrap_wiki(root)

    assert moved == 1
    assert (root / "wiki" / "alex" / "recipes" / "pizza.md").read_text() == "# Pizza\n"
    assert (root / "wiki" / "alex" / "note.md").read_text() == "already here\n"
    assert (legacy / "note.md").exists()  # a clash stays where it was
    assert not (legacy / "recipes").exists()


def test_validate_layout_wants_a_wiki(tmp_path) -> None:
    root = tmp_path / "data"
    layout.bootstrap_shared("Home", root)
    assert "wiki/ missing" in layout.validate_layout(root)
    layout.bootstrap_wiki(root)
    assert "wiki/ missing" not in layout.validate_layout(root)


# -- the writability check -------------------------------------------------


def test_validate_layout_reports_no_fault_for_a_writable_root(data_dir: Path) -> None:
    """The check writes a file. It does not read the mode bits, which can lie.

    On an NFS export the server decides who may write, so a data root core had
    written to for the life of the instance read as not writable and `/readyz`
    reported a fault that was not there.
    """
    layout.bootstrap_wiki()
    layout.bootstrap_shared("Test House")
    assert [p for p in layout.validate_layout() if "not writable" in p] == []


def test_validate_layout_still_reports_a_root_it_cannot_write(tmp_path: Path) -> None:
    """A path below a regular file can never be made, whatever the uid is."""
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    problems = layout.validate_layout(blocker / "data")
    assert problems == [f"data root missing: {blocker / 'data'}"]


def test_validate_layout_leaves_no_probe_file(data_dir: Path) -> None:
    layout.bootstrap_wiki()
    layout.bootstrap_shared("Test House")
    layout.validate_layout()
    assert [p.name for p in data_dir.iterdir() if p.name.startswith(".joshua-write-probe")] == []
