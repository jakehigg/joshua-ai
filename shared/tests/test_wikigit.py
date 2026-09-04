"""Tests for `joshua_shared.wikigit`, against the real `git` binary in a tmp
dir. Skipped, naming this file, when `git` is not on PATH."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from joshua_shared import wikigit

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary required — see test_wikigit.py"
)


def _log(wiki: Path) -> str:
    result = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True, check=True
    )
    return result.stdout


def test_ensure_repo_creates_repo_and_gitignore(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    assert wikigit.ensure_repo(wiki) is True
    assert (wiki / ".git").is_dir()
    assert (wiki / ".gitignore").read_text() == ".trash/\n"


def test_ensure_repo_is_idempotent(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wikigit.ensure_repo(wiki)
    (wiki / ".gitignore").write_text("mine\n")
    assert wikigit.ensure_repo(wiki) is False
    # A second call never overwrites an existing .gitignore.
    assert (wiki / ".gitignore").read_text() == "mine\n"


def test_commit_of_one_path_records_only_that_path(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wikigit.ensure_repo(wiki)
    (wiki / "a.md").write_text("a\n")
    (wiki / "b.md").write_text("b\n")

    assert wikigit.commit(wiki, [wiki / "a.md"], "write a") is True

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wiki, capture_output=True, text=True, check=True
    ).stdout
    assert "b.md" in status
    assert "a.md" not in status
    assert "write a" in _log(wiki)


def test_commit_with_nothing_changed_returns_false(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wikigit.ensure_repo(wiki)
    (wiki / "a.md").write_text("a\n")
    assert wikigit.commit(wiki, [wiki / "a.md"], "first") is True
    assert wikigit.commit(wiki, [wiki / "a.md"], "again") is False


def test_commit_rejects_a_path_outside_the_wiki(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wikigit.ensure_repo(wiki)
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x\n")
    with pytest.raises(ValueError, match="outside the wiki"):
        wikigit.commit(wiki, [outside], "nope")


def test_a_removed_path_is_committed_as_a_deletion(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wikigit.ensure_repo(wiki)
    target = wiki / "gone.md"
    target.write_text("bye\n")
    wikigit.commit(wiki, [target], "add gone")

    target.unlink()
    assert wikigit.commit(wiki, [target], "remove gone") is True
    status = subprocess.run(
        ["git", "show", "--stat", "HEAD"], cwd=wiki, capture_output=True, text=True, check=True
    ).stdout
    assert "gone.md" in status


def test_trash_content_is_not_committed(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wikigit.ensure_repo(wiki)
    wikigit.commit(wiki, None, "init")  # commit .gitignore itself first
    trash = wiki / ".trash" / "20260101T000000Z"
    trash.mkdir(parents=True)
    (trash / "deleted.md").write_text("gone\n")

    assert wikigit.commit(wiki, None, "sync") is False
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=wiki, capture_output=True, text=True, check=True
    ).stdout
    assert status == ""


def test_a_simulated_index_lock_still_commits(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    wikigit.ensure_repo(wiki)
    (wiki / "a.md").write_text("a\n")

    lock = wiki / ".git" / "index.lock"
    lock.write_text("")

    def release() -> None:
        time.sleep(0.5)
        lock.unlink()

    threading.Thread(target=release).start()

    assert wikigit.commit(wiki, [wiki / "a.md"], "after lock") is True
    assert "after lock" in _log(wiki)


def test_git_missing_returns_false_and_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    monkeypatch.setenv("PATH", "")
    assert wikigit.ensure_repo(wiki) is False
    assert not (wiki / ".git").exists()
    assert wikigit.commit(wiki, None, "sync") is False
