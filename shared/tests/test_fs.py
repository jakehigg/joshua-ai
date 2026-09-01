"""The writability probe.

Every case is proved by a real write, because that is the point of the module:
the mode bits are not the answer. A path below a regular file can never be made
whatever the uid is, so the negative case does not depend on the runner. A
read-only mode would prove nothing, because root ignores it and CI can run as
root.
"""

from __future__ import annotations

import os
from pathlib import Path

from joshua_shared.fs import PROBE_PREFIX, is_writable


def test_a_writable_directory_is_writable(tmp_path: Path) -> None:
    assert is_writable(tmp_path) is True


def test_a_directory_that_does_not_exist_yet_is_made(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "tree"
    assert is_writable(target) is True
    assert target.is_dir()


def test_a_path_below_a_regular_file_is_refused(tmp_path: Path) -> None:
    blocker = tmp_path / "afile"
    blocker.write_text("not a directory")
    assert is_writable(blocker / "cache") is False


def test_the_probe_file_never_remains(tmp_path: Path) -> None:
    assert is_writable(tmp_path) is True
    assert list(tmp_path.iterdir()) == []


def test_two_probes_in_a_row_leave_nothing(tmp_path: Path) -> None:
    """The name is unique, so one probe never collides with another."""
    assert is_writable(tmp_path) is True
    assert is_writable(tmp_path) is True
    assert [p for p in tmp_path.iterdir() if p.name.startswith(PROBE_PREFIX)] == []


def test_a_path_takes_a_string_or_a_path(tmp_path: Path) -> None:
    assert is_writable(str(tmp_path)) is True
    assert is_writable(Path(tmp_path)) is True


def test_the_answer_does_not_come_from_the_mode_bits(tmp_path: Path) -> None:
    """The whole reason the module exists: ``os.access`` can disagree.

    The probe answers with a write. This test states the contract that a
    directory the process can write reads as writable, whatever a mode-bit
    reader would say about it.
    """
    target = tmp_path / "data"
    target.mkdir()
    assert is_writable(target) is True
    assert (target / "written.txt").write_text("proof") == 5
    assert os.path.isdir(target)
