"""Git over the wiki, through the `git` binary only — no new dependency.

Joshua keeps `/data/wiki` as a git repository, so a wiki frontend such as
Otter Wiki never shows a page as "not under version control". `ensure_repo`
makes the repository once; `commit` records what Joshua wrote, and anything
else that changed, at start and at the nightly run. See `wiki.git` in
`joshua_shared.config`.

The repository is shared with a frontend that commits its own edits, so a
commit here can race one from that frontend and hit a transient
`index.lock`. `commit` retries that case; every other failure, or `git`
itself missing, is a WARNING, never an exception — a commit must never
break the write it follows. The one exception is a path outside the wiki,
which is a programming error and raises `ValueError`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from joshua_shared.log import get_logger

logger = get_logger("wikigit")

_TIMEOUT_S = 30
_GITIGNORE_BODY = ".trash/\n"
# Retries for a transient `index.lock` from a frontend committing at the same
# moment: up to five, with a growing sleep (0.2s, 0.4s, ...).
_LOCK_RETRIES = 5
_LOCK_BASE_DELAY_S = 0.2

_COMMIT_ENV = [
    "-c",
    "user.name=Joshua",
    "-c",
    "user.email=joshua@localhost",
    "-c",
    "commit.gpgsign=false",
]


def is_enabled(settings: Any) -> bool:
    """True when `wiki.git` is on (the default)."""
    return bool(settings.wiki.git)


def ensure_repo(wiki: Path) -> bool:
    """Make `wiki` a git repository if it is not one yet. Idempotent.

    Writes `wiki/.gitignore` with `.trash/` when the file is absent, and
    never overwrites one that is already there. Returns True when this call
    created the repository, False when one was already there or `git`
    failed (logged as a WARNING).
    """
    try:
        return _ensure_repo(wiki)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning({"message": "wiki git init failed", "error": str(exc)})
        return False


def commit(wiki: Path, paths: Iterable[Path | str] | None, message: str) -> bool:
    """Stage and commit `paths` (or everything, when `paths` is None) in the
    wiki repository. Returns True when a commit was made, False when there
    was nothing to commit or `git` failed (logged as a WARNING).

    Raises `ValueError` when a path is not inside `wiki`: a caller passing a
    path outside the wiki is a programming error, not a runtime failure. A
    path that no longer exists (a rename source, a delete) is still valid —
    it is passed to `git add -A` so the removal is staged.
    """
    rel_paths = None if paths is None else [_relative(wiki, p) for p in paths]
    try:
        return _commit(wiki, rel_paths, message)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning({"message": "wiki git commit failed", "error": str(exc)})
        return False


# --- internals ---------------------------------------------------------


def _relative(wiki: Path, path: Path | str) -> str:
    wiki_real = wiki.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = wiki / candidate
    candidate_real = candidate.resolve()
    try:
        return str(candidate_real.relative_to(wiki_real))
    except ValueError as exc:
        raise ValueError(f"path outside the wiki: {path}") from exc


def _ensure_repo(wiki: Path) -> bool:
    if (wiki / ".git").exists():
        _write_gitignore(wiki)
        return False
    if shutil.which("git") is None:
        logger.warning({"message": "git not found; cannot init the wiki repository"})
        return False

    wiki.mkdir(parents=True, exist_ok=True)
    result = _git_run(["init", "-q", "-b", "main"], cwd=wiki)
    if result.returncode != 0:
        # An older git has no `-b` on `init`. Fall back to a plain init, then
        # point HEAD at `main` directly.
        result = _git_run(["init", "-q"], cwd=wiki)
        if result.returncode != 0:
            _log_failure("init", result)
            return False
        symref = _git_run(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=wiki)
        if symref.returncode != 0:
            _log_failure("symbolic-ref", symref)
            return False

    _write_gitignore(wiki)
    return True


def _write_gitignore(wiki: Path) -> None:
    gitignore = wiki / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE_BODY)


def _commit(wiki: Path, rel_paths: list[str] | None, message: str) -> bool:
    if shutil.which("git") is None:
        logger.warning({"message": "git not found; skipping the wiki commit"})
        return False

    if rel_paths is None:
        add_result = _git_run(["add", "-A"], cwd=wiki)
        if add_result.returncode != 0:
            _log_failure("add", add_result)
            return False
    elif rel_paths:
        add_result = _git_run(["add", "-A", "--", *rel_paths], cwd=wiki)
        if add_result.returncode != 0:
            _log_failure("add", add_result)
            return False
    # An empty ``rel_paths`` list stages nothing; the commit below then finds
    # nothing new and reports it, same as any other empty change.

    result = _git_run([*_COMMIT_ENV, "commit", "-q", "-m", message], cwd=wiki)
    if result.returncode == 0:
        return True
    if _nothing_to_commit(result):
        return False
    _log_failure("commit", result)
    return False


def _nothing_to_commit(result: subprocess.CompletedProcess[str]) -> bool:
    text = (result.stdout or "") + (result.stderr or "")
    return "nothing to commit" in text


def _git_run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one git command, retrying a transient `index.lock` up to
    `_LOCK_RETRIES` times with a growing sleep. Never raises for a normal git
    failure — the exit code and stderr say so; may raise `OSError` or
    `subprocess.TimeoutExpired` when `git` itself cannot run.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    delay = _LOCK_BASE_DELAY_S
    attempt = 0
    while True:
        # The volume can carry an owner git does not expect (an NFS export,
        # a restore), and git then refuses the repository as "dubious".
        # Trust the wiki directory alone, for this call alone.
        result = subprocess.run(
            ["git", "-c", f"safe.directory={cwd}", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            env=env,
        )
        if result.returncode == 0:
            return result
        locked = result.returncode == 128 and "index.lock" in (result.stderr or "")
        if locked and attempt < _LOCK_RETRIES:
            time.sleep(delay)
            delay += _LOCK_BASE_DELAY_S
            attempt += 1
            continue
        return result


def _log_failure(op: str, result: subprocess.CompletedProcess[str]) -> None:
    stderr_first = (result.stderr or "").splitlines()[0] if result.stderr else ""
    logger.warning(
        {
            "message": "wiki git command failed",
            "op": op,
            "exit_code": result.returncode,
            "stderr": stderr_first,
        }
    )
