"""A session, as the same user, cannot read the environment of the non-dumpable manager.

The test starts a manager process with ``DATABASE_URL``,
``ASSIGNMENTS_TOKENS``, and ``ADDON_TOKEN`` in its start environment. The
manager calls ``make_non_dumpable`` (or not, for the control case), then
starts a shell the way the runner starts the Claude CLI: the same user, the
environment of the manager plus ``session_env``, in the scratch directory.
The shell tries ``/proc/<manager pid>/environ``, ``maps``, and ``fd``,
prints its own environment, and writes a file in its ``HOME``.

It needs Linux. ``scripts/test-nondumpable.sh`` runs this file in the
runtime image with plain Docker, for a developer on another system. The
image has no pytest, so the file also runs as a script.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SECRETS = {
    "DATABASE_URL": "postgresql://joshua:fake-database-password@postgres.invalid:5432/joshua",
    "ASSIGNMENTS_TOKENS": "alex=fake-caller-token-for-alex",
    "ADDON_TOKEN": "fake-addon-token",
}

PROBE = r"""
pid=$1
for name in environ maps; do
  if err=$(cat "/proc/$pid/$name" 2>&1 >/dev/null); then echo "$name=read"
  else echo "$name=$err"; fi
done
if err=$(ls "/proc/$pid/fd" 2>&1 >/dev/null); then echo "fd=read"; else echo "fd=$err"; fi
echo probe > "$HOME/probe.txt"
for dir in /app /app/.venv /app/assignments/joshua_assignments; do
  if err=$( (echo x > "$dir/probe") 2>&1); then echo "write:$dir=written"
  else echo "write:$dir=$err"; fi
done
echo "owner:/app=$(stat -c %u /app 2>/dev/null)"
echo "--env--"
env
"""

# The manager: harden (or not), then start the session as the runner does.
MANAGER = r"""
import json, os, subprocess, sys
from pathlib import Path
from joshua_assignments import harden, session

work = Path(sys.argv[1])
if sys.argv[2] == "harden":
    harden.make_non_dumpable()
home = work / ".home"
config_dir = home / ".claude"
config_dir.mkdir(parents=True, exist_ok=True)
env = session.session_env(
    os.environ,
    core_url="http://core.invalid:8000",
    token="fleet-token-for-the-test",
    home=home,
    config_dir=config_dir,
)
# The environment the SDK gives the CLI: this process, then options.env.
child_env = {**os.environ, "CLAUDE_CODE_ENTRYPOINT": "sdk-py", **env}
out = subprocess.run(
    ["sh", "-c", sys.argv[3], "probe", str(os.getpid())],
    cwd=work, env=child_env, capture_output=True, text=True, check=True,
).stdout
print(json.dumps({"out": out}))
"""


def run_session(harden: bool) -> tuple[dict[str, str], str, Path]:
    """Start a manager and its session. Returns the probe lines, the session env, and HOME."""
    work = Path(tempfile.mkdtemp(prefix="nd")) / "1"
    work.mkdir()
    env = {**os.environ, **SECRETS, "PYTHONPATH": os.pathsep.join(sys.path)}
    done = subprocess.run(
        [sys.executable, "-c", MANAGER, str(work), "harden" if harden else "plain", PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])["out"]
    head, _, env_text = out.partition("--env--\n")
    lines = dict(line.split("=", 1) for line in head.splitlines() if "=" in line)
    return lines, env_text, work / ".home"


def check_hardened() -> None:
    lines, env_text, home = run_session(harden=True)
    for name in ("environ", "maps", "fd"):
        assert "Permission denied" in lines[name], f"{name}: {lines[name]}"
    session_env = dict(line.split("=", 1) for line in env_text.splitlines() if "=" in line)
    for name, value in SECRETS.items():
        # The runner gives each secret an empty value in the session.
        assert session_env.get(name, "") == "", f"{name} has a value in the session"
        assert value not in env_text, f"the value of {name} is in the session environment"
    assert (home / "probe.txt").exists()
    for path in home.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            for name, value in SECRETS.items():
                assert value.encode() not in data, f"{path} holds {name}"


def check_control() -> None:
    """Without the call, the same session reads the environment: the test means something."""
    lines, _, _ = run_session(harden=False)
    assert lines["environ"] == "read", lines["environ"]


IN_IMAGE = Path("/app/.venv").is_dir() and sys.prefix.startswith("/app/")


def check_code_read_only() -> None:
    """In the image: the session cannot change the code that the manager runs."""
    lines, _, _ = run_session(harden=True)
    assert lines["owner:/app"] == "0", "/app must belong to root"
    for path in ("/app", "/app/.venv", "/app/assignments/joshua_assignments"):
        assert "Permission denied" in lines[f"write:{path}"], lines[f"write:{path}"]


# --- pytest ------------------------------------------------------------------

try:
    import pytest
except ImportError:  # the runtime image: run as a script
    pytest = None

if pytest is not None:
    pytestmark = pytest.mark.skipif(
        sys.platform != "linux",
        reason="needs Linux /proc; scripts/test-nondumpable.sh runs it in Docker (#75)",
    )

    def test_a_session_cannot_read_the_non_dumpable_manager() -> None:
        check_hardened()

    def test_without_the_call_the_session_can_read_it() -> None:
        check_control()

    @pytest.mark.skipif(not IN_IMAGE, reason="needs the /app of the image (#75)")
    def test_the_session_cannot_change_the_code_in_the_image() -> None:
        check_code_read_only()


if __name__ == "__main__":
    if sys.platform != "linux":
        sys.exit("needs Linux (#75)")
    check_control()
    print("control: a session reads the environment of a dumpable manager")
    check_hardened()
    print(
        "hardened: environ, maps, and fd are denied (EACCES); "
        "no secret in the session environment or under HOME"
    )
    if IN_IMAGE:
        check_code_read_only()
        print("code: /app belongs to root; the session cannot write /app or /app/.venv")
    else:
        sys.exit("not in the image: the check of /app did not run")
