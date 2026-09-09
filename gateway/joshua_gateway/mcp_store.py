"""Install a ``package:`` MCP server into the gateway's store volume.

The store is derived data, not the data volume: one directory per catalog entry
under ``JOSHUA_MCP_STORE`` (``/opt/joshua-mcp``), each holding that entry's
install and an ``installed.json`` record. Nothing else writes there.

An install runs once. The record holds the spec, so a restart with no network
starts the server again from the store. A spec change installs again over the
same directory, with the working install moved aside and put back when the new
one fails, so a version bump that cannot install leaves the version you are
running. Each entry is independent: a failed install stops that entry only.

Every external command goes through ``runner`` and every download through
``download``, so the tests drive the whole flow with no network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from joshua_shared.log import get_logger
from joshua_shared.mcp_package import PackageSpec, default_command, parse_package

logger = get_logger("gateway.mcp_store")

STORE_ENV_VAR = "JOSHUA_MCP_STORE"
DEFAULT_STORE = "/opt/joshua-mcp"
RECORD_NAME = "installed.json"

# The interpreter a `pypi:` tool is installed against. The runtime image is a
# python:3.13 image, so the tool runs on the same interpreter as the gateway.
TOOL_PYTHON = "python3.13"

# An install is a network operation on a cold cache. Long enough for a large
# package, short enough that a hung registry does not pin the entry forever.
INSTALL_TIMEOUT_S = 600.0
DOWNLOAD_CHUNK = 1 << 16

# One in-process lock per entry name. Two instances of an identity server share
# one install, so only one of them may run it.
_locks: dict[str, asyncio.Lock] = {}


class InstallError(RuntimeError):
    """An install that did not finish. The message is one line, with no token."""


@dataclass(frozen=True)
class InstallRequest:
    """What to install for one catalog entry."""

    name: str  # the catalog entry name, which is the store directory name
    spec: PackageSpec
    registry: str | None = None
    allow_scripts: bool = False

    @classmethod
    def from_connect_cfg(cls, name: str, cfg: dict[str, Any]) -> InstallRequest | None:
        """Build a request from a stdio connect config, or None when it has no package."""
        raw = cfg.get("package")
        if not raw:
            return None
        return cls(
            name=name,
            spec=parse_package(raw, cfg.get("sha256")),
            registry=cfg.get("registry"),
            allow_scripts=bool(cfg.get("allow_scripts")),
        )


def store_root() -> Path:
    return Path(os.environ.get(STORE_ENV_VAR) or DEFAULT_STORE)


def entry_dir(name: str) -> Path:
    return store_root() / name


def read_record(name: str) -> dict[str, Any] | None:
    """Return the install record for ``name``, or None when it is absent or broken."""
    path = entry_dir(name) / RECORD_NAME
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def is_current(request: InstallRequest, record: dict[str, Any] | None) -> bool:
    """True when ``record`` was made by this exact request and its files are still there."""
    if record is None:
        return False
    if record.get("package") != request.spec.raw:
        return False
    if record.get("registry") != request.registry:
        return False
    if bool(record.get("allow_scripts")) != request.allow_scripts:
        return False
    if record.get("sha256") != request.spec.sha256:
        return False
    root = entry_dir(request.name)
    return all((root / rel).is_dir() for rel in record.get("bin_dirs", []))


def bin_paths(name: str, record: dict[str, Any]) -> list[Path]:
    """The absolute bin directories this install adds to the child's PATH."""
    root = entry_dir(name)
    return [root / rel for rel in record.get("bin_dirs", [])]


def resolve_command(name: str, record: dict[str, Any], command: str | None) -> str:
    """The executable to start for this entry.

    A configured ``command`` is looked up in the install's bin directories first,
    so ``command: python`` on a git package finds the project's own interpreter.
    A command the install does not carry is left as written and resolved on PATH.
    With no ``command``, the install must hold exactly one executable, or one
    named after the package.
    """
    dirs = bin_paths(name, record)
    if command:
        if "/" in command:
            return command
        for directory in dirs:
            candidate = directory / command
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return command

    found: list[Path] = []
    for directory in dirs:
        if not directory.is_dir():
            continue
        found += sorted(p for p in directory.iterdir() if p.is_file() and os.access(p, os.X_OK))
    if not found:
        raise InstallError(f"{name}: the install has no executable; set 'command'")
    if len(found) == 1:
        return str(found[0])
    wanted = record.get("bare_name")
    for path in found:
        if path.name == wanted:
            return str(path)
    names = ", ".join(sorted(p.name for p in found)[:8])
    raise InstallError(f"{name}: the install has several executables ({names}); set 'command'")


# -- the command runner ------------------------------------------------------


def _run(argv: list[str], cwd: Path, env: dict[str, str]) -> None:
    """Run one install command. Raises ``InstallError`` with the last output line."""
    try:
        result = subprocess.run(  # noqa: S603 — argv is built here, never from a person
            argv,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise InstallError(f"{argv[0]} is not in the gateway image") from exc
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"{argv[0]} timed out after {int(INSTALL_TIMEOUT_S)}s") from exc
    if result.returncode != 0:
        raise InstallError(f"{argv[0]} failed: {_last_line(result.stderr or result.stdout)}")


def _last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:300] if lines else "no output"


def _download(url: str, dest: Path) -> str:
    """Download ``url`` to ``dest`` and return the file's sha256."""
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=INSTALL_TIMEOUT_S) as response:  # noqa: S310
            with dest.open("wb") as out:
                while chunk := response.read(DOWNLOAD_CHUNK):
                    digest.update(chunk)
                    out.write(chunk)
    except OSError as exc:
        raise InstallError(f"download failed: {type(exc).__name__}") from exc
    return digest.hexdigest()


Runner = Callable[[list[str], Path, dict[str, str]], None]
Downloader = Callable[[str, Path], str]


def _base_env() -> dict[str, str]:
    """The environment an install command runs with. No fleet token reaches it."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        # The download caches sit beside the entries, not under HOME and not
        # inside an entry: a reinstall replaces the entry directory, and a cache
        # that survives it makes a version bump fast. A dot name cannot collide
        # with an entry, because an entry name starts with a letter.
        "npm_config_cache": str(store_root() / ".npm-cache"),
        "npm_config_update_notifier": "false",
        "UV_CACHE_DIR": str(store_root() / ".uv-cache"),
    }
    return env


# -- per-kind installs -------------------------------------------------------


def _install_npm(request: InstallRequest, target: Path, runner: Runner) -> dict[str, Any]:
    argv = [
        "npm",
        "install",
        "--prefix",
        str(target),
        "--no-audit",
        "--no-fund",
        "--omit=dev",
        "--install-strategy=hoisted",
    ]
    # A postinstall script runs code from the package at install time. It stays
    # off unless the entry asks for it, and docs/security.md says what that means.
    if not request.allow_scripts:
        argv.append("--ignore-scripts")
    if request.registry:
        argv += ["--registry", request.registry]
    argv.append(request.spec.requirement)
    runner(argv, target, _base_env())

    resolved = request.spec.version
    manifest = target / "node_modules" / request.spec.name / "package.json"
    try:
        resolved = json.loads(manifest.read_text(encoding="utf-8")).get("version", resolved)
    except (OSError, ValueError):
        pass
    return {
        "resolved": resolved,
        "bin_dirs": ["node_modules/.bin"],
        "lock_sha256": _file_sha256(target / "package-lock.json"),
    }


def _install_pypi(request: InstallRequest, target: Path, runner: Runner) -> dict[str, Any]:
    env = _base_env()
    env["UV_TOOL_DIR"] = str(target / "tools")
    env["UV_TOOL_BIN_DIR"] = str(target / "bin")
    argv = ["uv", "tool", "install", "--python", TOOL_PYTHON]
    if request.registry:
        argv += ["--default-index", request.registry]
    argv.append(request.spec.requirement)
    runner(argv, target, env)
    return {"resolved": request.spec.version, "bin_dirs": ["bin"]}


def _install_git(request: InstallRequest, target: Path, runner: Runner) -> dict[str, Any]:
    repo = target / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    env = _base_env()
    # A prompt in a non-interactive install hangs instead of failing.
    env["GIT_TERMINAL_PROMPT"] = "0"
    runner(["git", "init", "--quiet", "."], repo, env)
    runner(["git", "remote", "add", "origin", request.spec.name], repo, env)
    runner(["git", "fetch", "--quiet", "--depth", "1", "origin", request.spec.version], repo, env)
    runner(["git", "checkout", "--quiet", "FETCH_HEAD"], repo, env)

    sync = _base_env()
    sync["UV_PROJECT_ENVIRONMENT"] = str(repo / ".venv")
    runner(["uv", "sync", "--no-dev", "--python", TOOL_PYTHON], repo, sync)
    return {"resolved": request.spec.version, "bin_dirs": ["repo/.venv/bin"]}


def _install_url(request: InstallRequest, target: Path, download: Downloader) -> dict[str, Any]:
    bin_dir = target / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    filename = default_command(request.spec) or "server"
    dest = bin_dir / filename
    digest = download(request.spec.name, dest)
    if digest != request.spec.sha256:
        dest.unlink(missing_ok=True)
        raise InstallError(f"{request.name}: the download does not match sha256")
    dest.chmod(0o755)
    return {"resolved": digest, "bin_dirs": ["bin"]}


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# -- the install itself ------------------------------------------------------


def install(
    request: InstallRequest,
    *,
    runner: Runner = _run,
    download: Downloader = _download,
) -> dict[str, Any]:
    """Install ``request`` into its store directory. Returns the record.

    The install runs at the directory the server will run from, because npm and
    uv write that absolute path into a symlink and a shebang: an install built
    somewhere else and moved here points at a path that is gone. So the working
    install is moved aside first and put back when the new one fails, and a
    version bump that cannot install leaves the version you are running.
    """
    root = entry_dir(request.name)
    root.parent.mkdir(parents=True, exist_ok=True)
    previous = root.parent / f".{request.name}.previous"
    shutil.rmtree(previous, ignore_errors=True)
    if root.exists():
        root.rename(previous)
    staging = root
    staging.mkdir(parents=True)

    try:
        if request.spec.kind == "npm":
            detail = _install_npm(request, staging, runner)
        elif request.spec.kind == "pypi":
            detail = _install_pypi(request, staging, runner)
        elif request.spec.kind == "git":
            detail = _install_git(request, staging, runner)
        else:
            detail = _install_url(request, staging, download)
        record = {
            "name": request.name,
            "package": request.spec.raw,
            "kind": request.spec.kind,
            "bare_name": request.spec.bare_name,
            "registry": request.registry,
            "allow_scripts": request.allow_scripts,
            "sha256": request.spec.sha256,
            "installed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            **detail,
        }
        (staging / RECORD_NAME).write_text(json.dumps(record, indent=2), encoding="utf-8")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if previous.exists():  # put the working install back
            previous.rename(root)
        raise

    shutil.rmtree(previous, ignore_errors=True)
    logger.info(
        {
            "message": "mcp package installed",
            "server": request.name,
            "package": request.spec.raw,
            "resolved": record.get("resolved"),
        }
    )
    return record


def ensure(
    request: InstallRequest,
    *,
    reinstall: bool = False,
    runner: Runner = _run,
    download: Downloader = _download,
) -> dict[str, Any]:
    """Return the install record, installing only when the store does not match.

    A store that already holds this exact spec is used as it is, so a restart with
    no network still starts the server.
    """
    record = read_record(request.name)
    if not reinstall and is_current(request, record):
        assert record is not None
        return record
    return install(request, runner=runner, download=download)


async def ensure_async(
    request: InstallRequest,
    *,
    reinstall: bool = False,
    runner: Runner = _run,
    download: Downloader = _download,
) -> dict[str, Any]:
    """``ensure`` off the event loop, one install at a time per entry name."""
    lock = _locks.setdefault(request.name, asyncio.Lock())
    async with lock:
        return await asyncio.to_thread(
            ensure, request, reinstall=reinstall, runner=runner, download=download
        )
