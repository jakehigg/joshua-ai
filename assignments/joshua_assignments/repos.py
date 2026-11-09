"""Repository names and the repository allowlist.

A repository is ``host/group/name`` in lower case. ``group`` can hold more
than one part, for a GitLab subgroup. A caller can give a URL (``https://``,
``ssh://``, or ``git@host:group/name``) or the short form. Both become the
short form. An error message never repeats the input, because a URL can hold
a credential.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

_PART_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\Z")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?\Z")
_SCP_RE = re.compile(r"^[^/@:]+@([^/:]+):(.+)\Z")


class RepoError(ValueError):
    """A repository name is not valid. The message does not repeat the input."""


def normalize_repo(raw: str) -> str:
    """Return ``raw`` as ``host/group/name``. Raises RepoError when it is not a repository."""
    text = (raw or "").strip()
    bad = RepoError("a repository is a URL or host/group/name, such as github.com/group/name")
    if not text:
        raise bad
    scp = _SCP_RE.match(text)
    if "://" in text:
        parts = urlsplit(text)
        if parts.scheme not in ("http", "https", "ssh", "git"):
            raise bad
        try:
            port = parts.port
        except ValueError:
            raise bad from None
        host = parts.hostname or ""
        if port is not None:
            host = f"{host}:{port}"
        path = parts.path
    elif scp:
        host, path = scp.group(1), scp.group(2)
    else:
        host, _, path = text.partition("/")
    host = host.lower()
    path = path.strip("/").lower()
    path = path.removesuffix(".git")
    segments = path.split("/") if path else []
    if not _HOST_RE.match(host) or "." not in host.split(":")[0] or len(segments) < 2:
        raise bad
    _, colon, port_text = host.partition(":")
    if colon and not 1 <= int(port_text) <= 65535:
        raise bad
    if any(not _PART_RE.match(s) or s in (".", "..") for s in segments):
        raise bad
    return "/".join([host, *segments])


def repo_allowed(repo: str, patterns: Iterable[str]) -> bool:
    """True when ``repo`` matches one of ``patterns``. ``*`` matches across ``/`` too."""
    return any(fnmatch.fnmatchcase(repo, pattern.strip().lower()) for pattern in patterns)


def in_scope(row: Mapping[str, Any]) -> list[str]:
    """The repositories of an assignment: those of its spec, then those that Joshua granted."""
    spec = row.get("spec") or {}
    out: list[str] = []
    for repo in [*(spec.get("repos") or []), *(row.get("granted_repos") or [])]:
        if isinstance(repo, str) and repo not in out:
            out.append(repo)
    return out
