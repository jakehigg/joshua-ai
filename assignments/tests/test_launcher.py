"""The session launcher and its wrapper, with a launcher thread of this user and /bin/sh as the CLI.

These tests check the protocol: the request, the refusals, the exit code,
the signals, and the umask. ``test_session_user.py`` checks the other user.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from joshua_assignments import launcher

WRAPPER_CODE = "import sys; from joshua_assignments.launcher import wrapper_main; " + (
    "sys.exit(wrapper_main())"
)


def test_the_wrapper_sends_its_arguments_cwd_and_env_without_empty_values() -> None:
    line = launcher.build_request(
        ["--output-format", "stream-json"],
        {
            "PATH": "/bin",
            "HOME": "/work/1/.home",
            "DATABASE_URL": "",
            "ADDON_TOKEN": "x",
            "JOSHUA_TOKEN_ASSIGNMENTS": "",
            "JOSHUA_TOKEN_CORE": "c",
            "ASSIGNMENTS_SESSION_SOCKET": "/run/s.sock",
            "CLAUDE_CODE_OAUTH_TOKEN": "fleet",
        },
        "/work/1",
    )
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    body = json.loads(line)
    assert body == {
        "op": "run",
        "argv": ["--output-format", "stream-json"],
        "cwd": "/work/1",
        "env": {"PATH": "/bin", "HOME": "/work/1/.home", "CLAUDE_CODE_OAUTH_TOKEN": "fleet"},
    }


def _request(**changes):
    body = {
        "op": "run",
        "argv": ["-c", "true"],
        "cwd": "/work/7",
        "env": {"HOME": "/work/7/.home", "CLAUDE_CONFIG_DIR": "/work/7/.home/.claude"},
    }
    body.update(changes)
    return body


def test_check_request_accepts_a_session_in_its_assignment_directory(tmp_path) -> None:
    (tmp_path / "7").mkdir()
    req = _request(
        cwd=str(tmp_path / "7"),
        env={
            "HOME": str(tmp_path / "7/.home"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "7/.home/.claude"),
            "DATABASE_URL": "postgresql://x",
            "TERM": "",
        },
    )
    args, env, cwd = launcher.check_request(req, tmp_path)
    assert args == ["-c", "true"] and cwd == str(tmp_path / "7")
    assert "DATABASE_URL" not in env and "TERM" not in env


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"op": "kill"}, "not a run"),
        ({"argv": "sh"}, "argv"),
        ({"argv": [1]}, "argv"),
        ({"env": []}, "env"),
        ({"env": {"HOME": 1}}, "env"),
        ({"cwd": "work/7"}, "not absolute"),
        ({"cwd": "/etc"}, "not in the work directory"),
        ({"cwd": "/work"}, "directory of an assignment"),
        ({"cwd": "/work/x"}, "directory of an assignment"),
        ({"cwd": "/work/7/../../etc"}, "not in the work directory"),
        ({"env": {"HOME": "/work/8/.home", "CLAUDE_CONFIG_DIR": "/work/7/c"}}, "HOME"),
        ({"env": {"HOME": "/work/7/.home"}}, "not absolute"),
    ],
)
def test_check_request_refuses_a_request_outside_one_assignment(changes, message) -> None:
    with pytest.raises(launcher.Refused, match=message):
        launcher.check_request(_request(**changes), Path("/work"))
    with pytest.raises(launcher.Refused, match="not a run"):
        launcher.check_request(["run"], Path("/work"))


def test_exit_codes() -> None:
    assert launcher._exit_code(3) == 3
    assert launcher._exit_code(-signal.SIGTERM) == 128 + signal.SIGTERM


# --- the wrapper and the launcher, end to end -----------------------------------


class Launcher:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.work = root / "work"
        self.socket = str(root / "l.sock")
        self.codes: list[int] = []
        self.server = launcher.listen(self.socket)
        sh = shutil.which("sh")
        assert sh is not None
        self.thread = threading.Thread(target=self._serve, args=(sh,), daemon=True)
        self.thread.start()

    def _serve(self, cli: str) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return

            def run(conn=conn) -> None:
                with conn:
                    self.codes.append(launcher.handle(conn, cli, self.work))

            threading.Thread(target=run, daemon=True).start()

    def assignment(self, n: int = 1) -> Path:
        work = self.work / str(n)
        (work / ".home" / ".claude").mkdir(parents=True, exist_ok=True)
        return work

    def env(self, work: Path, **extra: str) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": os.pathsep.join(sys.path),
            "HOME": str(work / ".home"),
            "CLAUDE_CONFIG_DIR": str(work / ".home" / ".claude"),
            "ASSIGNMENTS_SESSION_SOCKET": self.socket,
            **extra,
        }

    def wrapper(self, work: Path, script: str, **kw) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-c", WRAPPER_CODE, "-c", script],
            cwd=work,
            env=kw.pop("env", None) or self.env(work),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **kw,
        )


@pytest.fixture
def served() -> Iterator[Launcher]:
    # A short directory: a Unix socket path has a limit of about 100 bytes.
    root = Path(tempfile.mkdtemp(prefix="jl", dir="/tmp"))
    served = Launcher(root)
    try:
        yield served
    finally:
        served.server.close()
        shutil.rmtree(root, ignore_errors=True)


def _wait_for(check, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_the_session_gets_the_pipes_the_cwd_and_the_code(served) -> None:
    work = served.assignment()
    proc = served.wrapper(
        work,
        'pwd; echo "home=$HOME"; echo "db=${DATABASE_URL-unset}"; '
        'echo "token=${JOSHUA_TOKEN_CORE-unset}"; echo "sock=${ASSIGNMENTS_SESSION_SOCKET-unset}"; '
        "mkdir made; mkdir -m 700 private; echo x > private/f; chmod 600 private/f; exit 3",
        env=served.env(work, DATABASE_URL="postgresql://x", JOSHUA_TOKEN_CORE="c", EMPTY=""),
    )
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 3, err
    lines = out.splitlines()
    assert Path(lines[0]).resolve() == work.resolve()
    assert lines[1:] == [
        f"home={work / '.home'}",
        "db=unset",
        "token=unset",
        "sock=unset",
    ]
    assert (work / "made").stat().st_mode & 0o777 == 0o770, "the session umask is 007"
    assert (work / "private").stat().st_mode & 0o770 == 0o770, "the launcher shares the tree"
    assert (work / "private" / "f").stat().st_mode & 0o777 == 0o660


def test_share_tree_skips_a_link_and_an_entry_of_another_user(tmp_path, monkeypatch) -> None:
    outside = tmp_path / "outside"
    outside.write_text("x")
    outside.chmod(0o600)
    root = tmp_path / "7"
    (root / "d").mkdir(parents=True)
    (root / "d" / "f").write_text("x")
    (root / "d" / "f").chmod(0o600)
    (root / "link").symlink_to(outside)
    (root / "d").chmod(0o000)
    assert launcher.share_tree(root) >= 3
    assert (root / "d").stat().st_mode & 0o770 == 0o770
    assert (root / "d" / "f").stat().st_mode & 0o777 == 0o660
    assert outside.stat().st_mode & 0o777 == 0o600, "a link is never followed"
    assert launcher.share_tree(root) == 0
    (root / "d" / "f").chmod(0o600)
    monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)
    assert launcher.share_tree(root) == 0
    assert launcher.share_tree(tmp_path / "missing") == 0


def test_a_request_outside_the_work_directory_is_refused(served) -> None:
    proc = served.wrapper(served.root, "echo ran")
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == launcher.EXIT_REFUSED
    assert out == "" and "refused" in err


def test_no_launcher_is_exit_127(served) -> None:
    work = served.assignment()
    env = served.env(work, ASSIGNMENTS_SESSION_SOCKET=str(served.root / "none.sock"))
    proc = served.wrapper(work, "echo ran", env=env)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == launcher.EXIT_NOT_STARTED
    assert out == "" and "cannot reach the session launcher" in err


def test_sigterm_reaches_the_session(served) -> None:
    work = served.assignment()
    proc = served.wrapper(work, 'trap "exit 7" TERM; echo ready; sleep 30 & wait')
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=30)
    assert proc.returncode == 7


def test_the_session_stops_when_the_wrapper_dies(served) -> None:
    work = served.assignment()
    proc = served.wrapper(
        work, 'trap "touch $HOME/stopped; exit 0" TERM; echo ready; sleep 30 & wait'
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    proc.kill()
    proc.wait(timeout=30)
    assert _wait_for(lambda: (work / ".home" / "stopped").exists())


def test_a_cli_that_does_not_start_is_exit_127(served) -> None:
    work = served.assignment()
    left, right = socket.socketpair()
    with left, right, open(os.devnull, "rb+") as null:
        fd = null.fileno()
        line = launcher.build_request([], served.env(work), str(work))
        socket.send_fds(left, [line], [fd, fd, fd])
        code = launcher.handle(right, str(served.root / "no-cli"), served.work)
        assert code == launcher.EXIT_NOT_STARTED
        assert json.loads(left.recv(1024)) == {"exit": 127, "error": "not_started"}


def test_a_request_without_the_pipes_is_refused(served) -> None:
    work = served.assignment()
    left, right = socket.socketpair()
    with left, right:
        left.sendall(launcher.build_request([], served.env(work), str(work)))
        assert launcher.handle(right, "/bin/sh", served.work) == launcher.EXIT_REFUSED


def test_main_refuses_root_and_a_missing_cli(monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(SystemExit):
        launcher.main(["--cli", "sh"])
    monkeypatch.setattr(os, "geteuid", lambda: 1001)
    with pytest.raises(SystemExit):
        launcher.main(["--cli", "no-such-cli-for-joshua"])


def test_main_serves_the_socket(monkeypatch) -> None:
    root = Path(tempfile.mkdtemp(prefix="jm", dir="/tmp"))
    seen: dict = {}

    def fake_serve(server, cli, work_root, logger=None) -> None:
        seen.update(cli=cli, work_root=work_root, mode=os.stat(root / "s.sock").st_mode)
        server.close()

    monkeypatch.setattr(launcher, "serve", fake_serve)
    try:
        launcher.main(["--socket", str(root / "s.sock"), "--cli", "sh", "--work-root", "/w"])
    finally:
        shutil.rmtree(root, ignore_errors=True)
    assert seen["cli"] == shutil.which("sh") and seen["work_root"] == Path("/w")
    assert seen["mode"] & 0o777 == 0o660


def test_serve_ends_when_the_socket_closes(served) -> None:
    server = launcher.listen(str(served.root / "x.sock"))
    server.close()
    launcher.serve(server, "/bin/sh", served.work)
