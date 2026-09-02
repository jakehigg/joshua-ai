"""Filesystem facts the three containers need, answered by trying them.

One question lives here: can this process write in this directory? The answer
comes from a probe file, never from ``os.access``.

``os.access`` reads the mode bits of the local inode. On an NFS export the
server decides who may write, and the mode bits the client sees do not carry
that decision, so the answer is wrong. A data root the process had written to
for the life of an instance read as not writable, and ``/readyz`` reported a
fault that was not there.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

# The prefix of the probe file. It is removed in every path out of the
# function, and the name says what made it if one ever survives a hard kill.
PROBE_PREFIX = ".joshua-write-probe-"


def is_writable(path: Path | str) -> bool:
    """True when ``path`` exists or can be made, and accepts a new file.

    Makes the directory, writes a file in it, and removes the file. The result
    is the fact and not a prediction of it, so an NFS export, an ACL, and a
    read-only mount all give the right answer. The probe file never remains.
    """
    target = str(path)
    handle: int | None = None
    probe = ""
    try:
        os.makedirs(target, exist_ok=True)
        handle, probe = tempfile.mkstemp(prefix=PROBE_PREFIX, dir=target)
        return True
    except OSError:
        return False
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):
                os.close(handle)
        if probe:
            with contextlib.suppress(OSError):
                os.unlink(probe)
