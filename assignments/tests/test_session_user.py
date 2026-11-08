"""A session runs as the ``session`` user and cannot read the secrets of the manager.

These tests need Linux and a session launcher that runs as another user, as
in production. ``scripts/test-session-user.sh`` builds the image and starts
them in Docker: a launcher container as uid 1001, and this test as uid 1000
in the same process namespace, so the session can see the manager process.
The test process is the manager. It starts with ``DATABASE_URL``,
``ASSIGNMENTS_TOKENS``, and ``ADDON_TOKEN`` in its environment, and the
variables that name the launchers:

- ``ASSIGNMENTS_SESSION_SOCKET``: the launcher of the real Claude CLI;
- ``ASSIGNMENTS_TEST_SHELL_SOCKET``: a launcher whose CLI is ``sh``, so that
  a test can run shell commands in a session, as the Bash tool does.

Elsewhere, each test is skipped with the reason.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from joshua_assignments import launcher, session
from joshua_assignments.sdk_runner import SdkRunner

SECRETS = ("DATABASE_URL", "ASSIGNMENTS_TOKENS", "ADDON_TOKEN")
SESSION_UID = 1001
SHELL_SOCKET_ENV = "ASSIGNMENTS_TEST_SHELL_SOCKET"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="the session user needs Linux: /proc and a second uid (#75)"
)

PROBE = r"""
pid=$1; start=$2
echo "uid=$(id -u)"
if [ -r "/proc/$pid/stat" ] && [ "$(cut -d' ' -f22 "/proc/$pid/stat")" = "$start" ]; then
  echo "manager=visible"
else
  echo "manager=hidden"
fi
if err=$(cat "/proc/$pid/environ" 2>&1 >/dev/null); then
  echo "environ=read"
else
  echo "environ=denied: $err"
fi
echo "probe" > "$HOME/probe.txt"
mkdir -p "$HOME/made/deeper" && echo x > "$HOME/made/deeper/file"
echo "--env--"
env
"""


def _socket_or_skip(name: str) -> str:
    path = os.environ.get(name, "")
    if not path or not os.path.exists(path) or not stat.S_ISSOCK(os.stat(path).st_mode):
        pytest.skip(
            f"needs a session launcher as another user at ${name}; "
            "scripts/test-session-user.sh starts one (#75)"
        )
    return path


def _secret_values() -> dict[str, str]:
    """The secrets of this process, which must be in its start environment."""
    start = Path("/proc/self/environ").read_bytes().split(b"\0")
    values = {name: os.environ.get(name, "") for name in SECRETS}
    for name, value in values.items():
        assert value, f"start the test with {name} set, so that the test means something"
        assert f"{name}={value}".encode() in start, f"{name} must be in the start environment"
    return values


def _start_time() -> str:
    return Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19]


def _runner(socket_path: str) -> SdkRunner:
    class Host:
        settings = None

    runner = SdkRunner(Host(), core=object(), work_root=launcher.WORK_ROOT)  # type: ignore[arg-type]
    assert Path(runner.cli_path).name == launcher.WRAPPER
    return runner


def _session_env(runner: SdkRunner, assignment_id: int, socket_path: str) -> tuple[Path, dict]:
    work, home, config_dir = runner.make_work(assignment_id)
    env = session.session_env(
        os.environ,
        core_url="http://core.invalid:8000",
        token="fleet-token-for-the-test",
        home=home,
        config_dir=config_dir,
        launcher_socket=socket_path,
    )
    return work, env


def _shell(socket_path: str, work: Path, env: dict[str, str], script: str, *args: str) -> str:
    """Run ``script`` in a session through the shell launcher, as the SDK starts the CLI."""
    # The environment the SDK gives the CLI: this process, then options.env.
    process_env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": "sdk-py", **env}
    process_env[launcher.SOCKET_ENV] = socket_path
    done = subprocess.run(
        [shutil.which(launcher.WRAPPER) or launcher.WRAPPER, "-c", script, "probe", *args],
        cwd=work,
        env=process_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _home_holds(socket_path: str, work: Path, env: dict[str, str], values: dict[str, str]):
    """The secrets that a file under the session HOME holds.

    The CLI makes some files with the mode 600, so the session reads them,
    not this process. The session gets no secret: it prints every file, and
    this process looks for the values.
    """
    dump = _shell(socket_path, work, env, 'find "$HOME" -type f -exec cat {} +; echo')
    return [name for name, value in values.items() if value in dump]


def test_a_shell_in_the_session_cannot_read_the_manager_secrets() -> None:
    socket_path = _socket_or_skip(SHELL_SOCKET_ENV)
    values = _secret_values()
    runner = _runner(socket_path)
    assignment_id = 900_000 + os.getpid()
    work, env = _session_env(runner, assignment_id, socket_path)
    out = _shell(socket_path, work, env, PROBE, str(os.getpid()), _start_time())
    head, _, env_text = out.partition("--env--\n")
    lines = dict(line.split("=", 1) for line in head.splitlines() if "=" in line)

    assert int(lines["uid"]) == SESSION_UID != os.getuid()
    assert not lines["environ"].startswith("read"), "the session read the manager environment"
    if lines["manager"] == "visible":
        assert "Permission denied" in lines["environ"], lines["environ"]
    names = {line.split("=", 1)[0] for line in env_text.splitlines() if "=" in line}
    for name, value in values.items():
        assert name not in names, f"{name} is in the session environment"
        assert value not in env_text, f"the value of {name} is in the session environment"
    home = work / ".home"
    assert (home / "probe.txt").stat().st_uid == SESSION_UID
    assert _home_holds(socket_path, work, env, values) == []

    runner.remove_work(assignment_id)
    assert not work.exists(), "the manager must remove what the session made"


async def test_the_wrapper_starts_the_real_cli_as_the_session_user() -> None:
    socket_path = _socket_or_skip(launcher.SOCKET_ENV)
    shell_socket = _socket_or_skip(SHELL_SOCKET_ENV)
    if shutil.which(launcher.DEFAULT_CLI) is None:
        pytest.skip("needs the Claude CLI on PATH (#75)")
    from claude_agent_sdk import ClaudeSDKClient

    values = _secret_values()
    runner = _runner(socket_path)
    assignment_id = 910_000 + os.getpid()
    work, env = _session_env(runner, assignment_id, socket_path)

    async def noop(args):
        return {"content": []}

    options = session.build_options(
        system_prompt="test",
        cwd=work,
        model="claude-sonnet-4-5",
        effort=None,
        max_turns=1,
        env=env,
        gateway={},
        gateway_url="http://gateway.invalid:8000",
        assignment=session.assignment_server(dict.fromkeys(session.TOOL_SPECS, noop)),
        resume=None,
        cli_path=runner.cli_path,
    )
    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        uids = _cli_uids()
    finally:
        await client.disconnect()
    home = work / ".home"
    owners = {p.stat().st_uid for p in home.rglob("*")}
    if uids:
        assert uids == {SESSION_UID}, uids
    else:
        assert owners, "the CLI made no file, and its process is not visible"
    assert owners <= {SESSION_UID, os.getuid()}
    assert SESSION_UID in owners or uids
    assert _home_holds(shell_socket, work, env, values) == []
    runner.remove_work(assignment_id)
    left = "" if not work.exists() else _shell(shell_socket, work, env, "find . -exec ls -ld {} +")
    assert not work.exists(), left


def _cli_uids() -> set[int]:
    """The uids of the visible processes of the Claude CLI, after a short wait."""
    for _ in range(50):
        uids: set[int] = set()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().split(b"\0")
                status = (entry / "status").read_text()
            except OSError:
                continue
            if any(part.endswith(b"/claude") or part == b"claude" for part in cmdline[:2]):
                uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
                uids.add(int(uid_line.split()[1]))
        if uids:
            return uids
        time.sleep(0.1)
    return set()
