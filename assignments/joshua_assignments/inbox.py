"""The inbox of an assignment: the files that Joshua gives a worker.

``provide_file`` puts one text file in ``inbox/`` of the scratch directory
of an assignment. The manager writes it, never the session, and only while
the assignment is parked, so no session runs. The file is data for the
worker. Nothing executes it.

The name is a plain file name: at most ``MAX_NAME`` characters, no path
separator, no leading dot, and no control character. The caps are
``MAX_FILE_BYTES`` for one file, ``MAX_TOTAL_BYTES`` for all the files of
one assignment, and ``MAX_FILES`` files. A file with the name of an earlier
one replaces it.

A session has Bash in the scratch directory, so it can put a symbolic link
where ``inbox/`` or a file of it should be. ``write`` opens the scratch
directory, ``inbox/``, and the file with ``O_NOFOLLOW`` relative to each
other, so a write never follows a link out of ``inbox/``.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

INBOX_DIR = "inbox"
MAX_NAME = 120
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_FILES = 50
_SEPARATORS = ("/", "\\")


class InboxRefused(ValueError):
    """A file that the inbox refuses. ``reason`` is a fixed word for the caller."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def check_name(raw: Any) -> str:
    """``raw`` when it is a plain file name. Raises InboxRefused ``invalid_name``."""
    if not isinstance(raw, str) or not raw.strip():
        raise InboxRefused("invalid_name", "name is a file name, such as manual.md")
    if len(raw) > MAX_NAME:
        raise InboxRefused("invalid_name", f"name is longer than {MAX_NAME} characters")
    if any(sep in raw for sep in _SEPARATORS):
        raise InboxRefused("invalid_name", "name has no path separator")
    if raw.startswith("."):
        raise InboxRefused("invalid_name", "name does not start with a dot")
    if raw != raw.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise InboxRefused("invalid_name", "name has no control character and no outer space")
    return raw


def records(raw: Any) -> list[dict[str, Any]]:
    """The inbox list of a row: each item a dict with a ``name``."""
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict) and isinstance(item.get("name"), str)]


def check_caps(current: Iterable[dict[str, Any]], name: str, size: int) -> None:
    """Raise InboxRefused ``too_large`` or ``too_many`` when the new file breaks a cap.

    A file with the name of an earlier one replaces it, so the earlier one
    does not count."""
    others = [item for item in current if item.get("name") != name]
    if size > MAX_FILE_BYTES:
        raise InboxRefused(
            "too_large", f"the file is {size} bytes; the cap is {MAX_FILE_BYTES} bytes per file"
        )
    total = sum(int(item.get("bytes") or 0) for item in others) + size
    if total > MAX_TOTAL_BYTES:
        raise InboxRefused(
            "too_large",
            f"the inbox would hold {total} bytes; the cap is {MAX_TOTAL_BYTES} bytes "
            "per assignment",
        )
    if len(others) + 1 > MAX_FILES:
        raise InboxRefused("too_many", f"the inbox holds {MAX_FILES} files, the cap")


def with_record(
    current: Iterable[dict[str, Any]], name: str, size: int, at: datetime
) -> list[dict[str, Any]]:
    """The inbox list with ``name`` added, or replaced when it was there."""
    kept = [dict(item) for item in current if item.get("name") != name]
    return [*kept, {"name": name, "bytes": int(size), "at": at.isoformat()}]


def write(work: Path, name: str, data: bytes) -> Path:
    """Write ``data`` to ``<work>/inbox/<name>`` and follow no link on the way.

    Raises InboxRefused ``bad_inbox`` when ``work`` or ``inbox/`` is not a
    plain directory, or when a directory has the name of the file."""
    check_name(name)
    work.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        work_fd = os.open(work, flags)
    except OSError:
        raise InboxRefused("bad_inbox", "the scratch directory is not a plain directory") from None
    try:
        try:
            os.mkdir(INBOX_DIR, 0o700, dir_fd=work_fd)
        except FileExistsError:
            pass
        try:
            inbox_fd = os.open(INBOX_DIR, flags, dir_fd=work_fd)
        except OSError:
            raise InboxRefused("bad_inbox", "inbox/ is not a plain directory") from None
        try:
            _write_in(inbox_fd, name, data)
        finally:
            os.close(inbox_fd)
    finally:
        os.close(work_fd)
    return work / INBOX_DIR / name


def _write_in(inbox_fd: int, name: str, data: bytes) -> None:
    try:
        os.unlink(name, dir_fd=inbox_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno in (errno.EISDIR, errno.EPERM, errno.ENOTEMPTY):
            raise InboxRefused("bad_inbox", f"inbox/{name} is a directory") from None
        raise
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=inbox_fd)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def missing(work: Path, current: Iterable[dict[str, Any]]) -> list[str]:
    """The names of the recorded files that are not in ``<work>/inbox/`` now."""
    folder = work / INBOX_DIR
    return [
        item["name"]
        for item in current
        if not (folder / item["name"]).is_file() or (folder / item["name"]).is_symlink()
    ]
