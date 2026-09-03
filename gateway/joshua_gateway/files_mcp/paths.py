"""Path confinement for the files MCP.

Every path is resolved against the root set of the request. A path that escapes
a root, through ``..``, an absolute path, or a symlink that points out of the
root, is rejected before any file operation runs. A ``PathError`` message is
safe to return to the caller: it names the root, never the resolved absolute
path.

**The corpus is one corpus.** The wiki is what Joshua knows, and that now
includes Joshua's own journal, under ``wiki/journal/``, and Joshua's profile of
each person, under ``wiki/people/``. ``people/<id>/`` holds only what another
container writes for a person: an attachment, from ``channels``, and the
terminal outbox, ``cli/``. Nobody writes there through this server; the agent
reads it, the same as the rest of the corpus.

The role decides whether a request writes at all. A member writes the wiki,
which now carries the journal, and a guest reads. A request with no role is a
guest.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# The person id for a request core could not attribute to a known person. The
# person is kept for the audit trail. It does not gate a path.
UNKNOWN = "unknown"

# The role that writes. Every other role reads.
MEMBER = "member"
GUEST = "guest"

# Roots that ``list_files`` and ``search_files`` accept by name.
DIR_ROOTS = ("wiki", "people", "shared")

# Root names a taught skill can still hold from an earlier layout, before the
# corpus moved onto one wiki. ``resolve`` names the replacement instead of
# "unknown root", so a person can correct the skill.
_RETIRED_ROOTS = {
    "profile.md": "wiki/people/<your person id>.md",
    "attachments": "people/<your person id>/attachments/",
}


class PathError(Exception):
    """A path escapes its root, names no root, or breaks a write rule.

    The message is safe to return to the caller; it never contains an absolute
    path.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Root:
    """One named, directory-backed root in a person's file tree.

    ``base`` is the absolute directory the root maps to. ``can_write`` and
    ``md_only`` gate ``write_file``.
    """

    name: str
    base: Path
    can_write: bool
    md_only: bool


def roots(data_root: Path, *, role: str) -> dict[str, Root]:
    """Return the roots a request with ``role`` may reach under ``data_root``.

    One corpus, three roots:

    - ``wiki``: what Joshua knows, including its own journal
      (``journal/``) and its profile of each person (``people/``). A member
      writes it.
    - ``people``: an attachment and a terminal outbox, one directory per
      person. Read-only through this server; ``channels`` owns the write.
    - ``shared``: the shared attachments. Read-only; ``core`` owns the write.

    ``role`` is ``member`` or anything else. Anything else, including a missing
    role and the literal ``unknown``, reads and writes nothing. That keeps the
    safe default: a request core could not attribute gets no write.
    """
    member = role == MEMBER
    return {
        "wiki": Root("wiki", data_root / "wiki", can_write=member, md_only=True),
        "people": Root("people", data_root / "people", can_write=False, md_only=False),
        "shared": Root("shared", data_root / "shared", can_write=False, md_only=False),
    }


def resolve(path: str, root_set: dict[str, Root], *, write: bool) -> tuple[Root, Path]:
    """Resolve a root-relative ``path`` to an absolute path inside its root.

    ``path`` starts with a root name, such as ``wiki/notes/x.md``,
    ``people/alex/attachments/2026/08/a.jpg``, or ``shared/team.jpg``. Raises
    ``PathError`` for an unknown root, a ``..`` or absolute path, a symlink that
    leaves the root, a segment that starts with a dot (a frontend's own state,
    such as ``.git`` or ``.trash``), a write to a read-only root, or a
    non-``.md`` write to a markdown root.
    """
    raw = path.strip()
    if not raw:
        raise PathError("empty path")
    if "\x00" in raw:
        raise PathError("invalid path")
    if raw.startswith("/") or raw.startswith("\\") or "\\" in raw:
        raise PathError("path must be relative to a root")
    parts = PurePosixPath(raw).parts
    if any(part == ".." for part in parts):
        raise PathError("path must not contain '..'")

    root = root_set.get(parts[0])
    if root is None:
        replacement = _RETIRED_ROOTS.get(parts[0])
        if replacement is not None:
            raise PathError(
                f"the root '{parts[0]}' is gone: the corpus is shared now. "
                f"Use {replacement} instead."
            )
        raise PathError(f"unknown or forbidden root: {parts[0]}")

    if any(part.startswith(".") for part in parts[1:]):
        # A wiki frontend or a note tool keeps its own state in a dot entry
        # (``.git``, ``.obsidian``, ``.trash``) inside the root. That state is
        # never part of the corpus, so a path naming it is forbidden the same
        # way an unknown root is.
        raise PathError(f"a hidden entry is forbidden in {root.name}")

    candidate = root.base.joinpath(*parts[1:])
    base_dir = root.base

    if write:
        if not root.can_write:
            raise PathError(f"{root.name} is read-only")
        if root.md_only and candidate.suffix != ".md":
            raise PathError("only .md files may be written")

    # realpath resolves every symlink in the existing prefix, so a link that
    # points out of the root fails the containment check below. A create write
    # targets a path that does not exist yet; realpath keeps the missing tail
    # literal, so the check still runs against the real parent.
    base_real = Path(os.path.realpath(base_dir))
    cand_real = Path(os.path.realpath(candidate))
    if not (cand_real == base_real or cand_real.is_relative_to(base_real)):
        raise PathError(f"path escapes {root.name}")
    return root, candidate
