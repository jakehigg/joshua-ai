"""The stall rule: an assignment that makes no progress stops and asks.

A slice makes progress when the session stores a new checkpoint in it.
``StallCounter`` counts the slices in a row with no new checkpoint, and keeps
the last tool errors of those slices. At ``assignments.stall_slices`` the
runner parks the assignment as ``waiting`` with ``question``: the last
checkpoint and those tool errors. ``answer_assignment`` resumes it as for
any question. A slice with a checkpoint sets the count to zero. ``finish``
and ``ask`` end the run, so the next run starts at zero.

A tool error goes into the question only through ``clean_error``: one line,
cut short, with each bearer and credential shape masked, the user part of a
URL removed, and each absolute path outside the scratch directory replaced
with ``<path>``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from joshua_shared.log import redact

MAX_ERROR = 300
MAX_ERRORS = 3
MAX_CHECKPOINT = 500
MAX_QUESTION = 2000
HIDDEN_PATH = "<path>"
# The user part of a URL: ``scheme://user:secret@``.
_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@")
# An absolute path that is not the path part of a URL.
_PATH_RE = re.compile(r"(?<![\w.:/~-])/[^\s'\"`,;:()<>\[\]{}]+")


def clean_error(text: str, work: Path) -> str:
    """``text`` as one short line with no token and no path outside ``work``."""
    root = str(work).rstrip("/")

    def path(match: re.Match[str]) -> str:
        found = match.group(0)
        return found if found == root or found.startswith(root + "/") else HIDDEN_PATH

    line = " ".join(text.split())
    line = _USERINFO_RE.sub(r"\1", line)
    line = _PATH_RE.sub(path, line)
    line = redact(line)
    return line if len(line) <= MAX_ERROR else line[: MAX_ERROR - 3] + "..."


class StallCounter:
    """The slices in a row with no new checkpoint, and their last tool errors."""

    def __init__(self, limit: int, work: Path) -> None:
        self.limit = max(1, int(limit))
        self.work = work
        self.slices = 0
        self.errors: list[str] = []

    def slice_ended(self, *, progress: bool, errors: Iterable[str]) -> bool:
        """Count one slice. True when the assignment must stop and ask."""
        if progress:
            self.slices = 0
            self.errors = []
            return False
        self.slices += 1
        for raw in errors:
            error = clean_error(raw, self.work)
            if not error:
                continue
            if error in self.errors:
                self.errors.remove(error)
            self.errors.append(error)
        self.errors = self.errors[-MAX_ERRORS:]
        return self.slices >= self.limit

    def question(self, checkpoint: str | None, page: str | None) -> str:
        """The automatic question of a stalled assignment."""
        slices = "1 slice" if self.slices == 1 else f"{self.slices} slices"
        lines = [
            f"I stopped because {slices} in a row ended with no new checkpoint, so I can be stuck."
        ]
        note = " ".join((checkpoint or "").split())
        if note:
            if len(note) > MAX_CHECKPOINT:
                note = note[: MAX_CHECKPOINT - 3] + "..."
            lines.append(f"My last checkpoint: {note}")
        else:
            lines.append("I have no checkpoint.")
        if self.errors:
            lines.append("The last tool errors:")
            lines += [f"- {error}" for error in self.errors]
        else:
            lines.append("I saw no tool error.")
        lines.append(f"My plan and progress are at {page}.")
        lines.append("Tell me how to continue, or stop me.")
        return "\n".join(lines)[:MAX_QUESTION]
