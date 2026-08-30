"""Path confinement for the files MCP.

Every path is resolved against the root set of the request. A path that escapes
a root, through ``..``, an absolute path, or a symlink that points out of the
root, is rejected before any file operation runs. A ``PathError`` message is
safe to return to the caller: it names the root, never the resolved absolute
path.

**The corpus is one corpus.** The wiki is what Joshua knows, the journal under
``people/<id>/blog/`` is when something happened, and an attachment is the
artifact. None of the three belongs to one person, so no root is keyed on a
person. ``people/<id>/`` says whose episode a journal entry records; it is
provenance, not a wall.

One rule decides a write: **the write domain**, which says the container that
owns a path. ``channels`` writes an attachment, ``core`` writes ``profile.md``,
and the agent writes the wiki and the journal. ``write_subdir`` carries the
rule. A member reads an attachment and does not write one, because ``channels``
owns that path.

The role decides the write and nothing else decides it. A member writes, a
guest reads, and a request with no role is a guest.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# The person id for a request core could not attribute to a known person. The
# person is kept for the audit trail and for the provenance of a journal entry.
# It does not gate a path.
UNKNOWN = "unknown"

# The role that writes. Every other role reads.
MEMBER = "member"
GUEST = "guest"

# Roots that ``list_files`` and ``search_files`` accept by name.
DIR_ROOTS = ("wiki", "people", "shared")

# The journal lives at ``people/<id>/blog/``. The agent writes there and nowhere
# else below ``people``, because ``core`` owns ``profile.md`` and ``channels``
# owns ``attachments/``.
JOURNAL_SUBDIR = "blog"

# Root names a taught skill can still hold from an earlier layout, when each
# person had a separate tree. ``resolve`` names the replacement instead of
# "unknown root", so a person can correct the skill.
_RETIRED_ROOTS = {
    "blog": f"people/<person>/{JOURNAL_SUBDIR}/",
    "profile.md": "people/<person>/profile.md",
    "attachments": "people/<person>/attachments/",
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
    """One named root in a person's file tree.

    ``base`` is the absolute directory (or file, when ``is_file``) the root maps
    to. ``can_write`` and ``md_only`` gate ``write_file``.
    """

    name: str
    base: Path
    can_write: bool
    md_only: bool
    is_file: bool = False
    write_subdir: str | None = None
    """The write domain inside a root that holds more than one owner's files.

    When set, a write must land at ``<root>/<first>/<write_subdir>/...``. The
    ``people`` root holds three kinds of file with three owners: the journal,
    which the agent writes; ``profile.md``, which ``core`` writes; and
    ``attachments/``, which ``channels`` writes. The role says whether this
    request may write at all. This says where such a write may land.
    """


def roots(data_root: Path, *, role: str) -> dict[str, Root]:
    """Return the roots a request with ``role`` may reach under ``data_root``.

    The set does not depend on which person sent the turn. One corpus, three
    roots:

    - ``wiki``: what Joshua knows. A member writes it.
    - ``people``: the journal, the profiles, and the attachments of everybody.
      A member writes the journal alone; see ``write_subdir``.
    - ``shared``: the shared profile and the group attachments. Nobody writes
      it through this server; ``core`` owns it.

    ``role`` is ``member`` or anything else. Anything else, including a missing
    role and the literal ``unknown``, reads and writes nothing. That keeps the
    safe default: a request core could not attribute gets no write.
    """
    member = role == MEMBER
    return {
        "wiki": Root("wiki", data_root / "wiki", can_write=member, md_only=True),
        "people": Root(
            "people",
            data_root / "people",
            can_write=member,
            md_only=True,
            write_subdir=JOURNAL_SUBDIR,
        ),
        "shared": Root("shared", data_root / "shared", can_write=False, md_only=True),
    }


def resolve(path: str, root_set: dict[str, Root], *, write: bool) -> tuple[Root, Path]:
    """Resolve a root-relative ``path`` to an absolute path inside its root.

    ``path`` starts with a root name, such as ``wiki/notes/x.md``,
    ``people/alex/blog/2026-08-29.md``, or ``shared/profile.md``. Raises
    ``PathError`` for an unknown root, a ``..`` or absolute path, a symlink that
    leaves the root, a write to a read-only root, a non-``.md`` write to a
    markdown root, or a write outside the write domain of the root.
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
            # A skill can hold a name from the earlier per-person layout. Name
            # the new path, so the person can correct the skill.
            raise PathError(
                f"the root '{parts[0]}' is gone: the corpus is shared now. "
                f"Use {replacement} instead."
            )
        raise PathError(f"unknown or forbidden root: {parts[0]}")

    if root.is_file:
        if len(parts) != 1:
            raise PathError(f"{root.name} is a file")
        candidate = root.base
        base_dir = root.base.parent
    else:
        candidate = root.base.joinpath(*parts[1:])
        base_dir = root.base

    if write:
        if not root.can_write:
            raise PathError(f"{root.name} is read-only")
        if root.md_only and candidate.suffix != ".md":
            raise PathError("only .md files may be written")
        if root.write_subdir is not None:
            # ``people/<person>/<write_subdir>/...``: parts[1] is the person and
            # parts[2] is the kind. A write anywhere else below ``people`` would
            # cross into the write domain of core or of channels.
            if len(parts) < 4 or parts[2] != root.write_subdir:
                raise PathError(f"only {root.name}/<person>/{root.write_subdir}/ may be written")

    # realpath resolves every symlink in the existing prefix, so a link that
    # points out of the root fails the containment check below. A create write
    # targets a path that does not exist yet; realpath keeps the missing tail
    # literal, so the check still runs against the real parent.
    base_real = Path(os.path.realpath(base_dir))
    cand_real = Path(os.path.realpath(candidate))
    if not (cand_real == base_real or cand_real.is_relative_to(base_real)):
        raise PathError(f"path escapes {root.name}")
    return root, candidate
