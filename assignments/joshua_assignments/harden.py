"""Make the manager process non-dumpable, so a session cannot read its memory or environment.

A session has a shell and runs as the same user as the manager. Linux lets a
process of the same user read ``/proc/<pid>/environ``, ``mem``, ``maps``, and
``fd`` of another process, unless that process is non-dumpable. Then the
kernel shows these files only to a process with CAP_SYS_PTRACE, and no
process in the container has it. ``make_non_dumpable`` calls
``prctl(PR_SET_DUMPABLE, 0)`` before the manager reads a secret or opens a
port. The Claude CLI of a session is a new program, so it is dumpable again;
that is correct, because it holds only the session environment.

On another system the call does nothing and logs one line.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from typing import Any

from joshua_shared.log import get_logger

logger = get_logger("joshua_assignments.harden")

PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4


def _libc() -> Any:
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def get_dumpable(libc: Any = None) -> int:
    """``prctl(PR_GET_DUMPABLE)``: 0 for a non-dumpable process. Linux only."""
    return int((libc or _libc()).prctl(PR_GET_DUMPABLE, 0, 0, 0, 0))


def make_non_dumpable(platform: str = sys.platform, libc: Any = None) -> bool:
    """Make this process non-dumpable. Returns True on Linux, False on another system.

    Raises OSError when the kernel refuses, because the manager must not run
    with a readable environment.
    """
    if platform != "linux":
        logger.info({"message": "not Linux: the manager process stays dumpable"})
        return False
    libc = libc or _libc()
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"prctl(PR_SET_DUMPABLE, 0) failed: {os.strerror(errno)}")
    return True
