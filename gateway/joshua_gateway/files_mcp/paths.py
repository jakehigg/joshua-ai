"""Path confinement for the files MCP.

Every path a person gives is resolved against that person's root set. A path that
escapes a root — through ``..``, an absolute path, or a symlink that points out of
the root — is rejected before any file operation runs. A ``PathError`` message is
safe to return to the caller: it names the root, never the resolved absolute path.

The person comes from the request, never from a tool argument. There is one
wiki. Everyone reads it; a member writes it; a guest reads it only. A request
with no person (or the literal ``unknown``) gets ``wiki`` and ``shared`` read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# The person id for a request that core could not attribute to a known person.
UNKNOWN = "unknown"

# Roots that ``list_files`` and ``search_files`` accept by name (``profile.md`` is
# a single file, reached only through ``read_file``).
DIR_ROOTS = ("wiki", "blog", "attachments", "shared")


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


def person_roots(data_root: Path, person: str | None, *, wiki_write: bool) -> dict[str, Root]:
    """Return the roots ``person`` may reach under ``data_root``.

    ``wiki`` is the one wiki at ``<data>/wiki``; everyone reads it and
    ``wiki_write`` (true for a member) grants writes. ``shared`` holds the
    shared profile and the group attachments and is read-only for everyone. A
    request with no person, or the literal ``unknown``, gets ``wiki`` and
    ``shared`` read and nothing else.
    """
    wiki = data_root / "wiki"
    shared = Root("shared", data_root / "shared", can_write=False, md_only=True)
    if person is None or person == UNKNOWN:
        return {
            "wiki": Root("wiki", wiki, can_write=False, md_only=True),
            "shared": shared,
        }
    home = data_root / "people" / person
    return {
        "wiki": Root("wiki", wiki, can_write=wiki_write, md_only=True),
        "blog": Root("blog", home / "blog", can_write=True, md_only=True),
        "profile.md": Root(
            "profile.md", home / "profile.md", can_write=False, md_only=False, is_file=True
        ),
        "attachments": Root("attachments", home / "attachments", can_write=False, md_only=False),
        "shared": shared,
    }


def resolve(path: str, roots: dict[str, Root], *, write: bool) -> tuple[Root, Path]:
    """Resolve a root-relative ``path`` to an absolute path inside its root.

    ``path`` starts with a root name, such as ``wiki/notes/x.md``, ``profile.md``,
    or ``attachments/2026/08/a.jpg``. Raises ``PathError`` for an unknown root, a
    ``..`` or absolute path, a symlink that leaves the root, a write to a read-only
    root, or a non-``.md`` write to a markdown root.
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

    root = roots.get(parts[0])
    if root is None:
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

    # realpath resolves every symlink in the existing prefix, so a link that
    # points out of the root fails the containment check below. A create write
    # targets a path that does not exist yet; realpath keeps the missing tail
    # literal, so the check still runs against the real parent.
    base_real = Path(os.path.realpath(base_dir))
    cand_real = Path(os.path.realpath(candidate))
    if not (cand_real == base_real or cand_real.is_relative_to(base_real)):
        raise PathError(f"path escapes {root.name}")
    return root, candidate
