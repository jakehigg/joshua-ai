"""The session launcher, and the wrapper that the SDK starts as the Claude CLI.

A session has a shell. If it runs as the user of the manager, a command in it
can read ``/proc/<manager pid>/environ``, which holds the database URL and
the bearers of the callers. A process with no capability cannot change its
user, and ``allowPrivilegeEscalation: false`` stops a setuid binary or a file
capability. So a second container, ``assignments-session``, starts this
launcher as the ``session`` user (uid 1001). The manager never runs as root
and holds no capability.

The two halves:

- ``wrapper_main`` is the ``joshua-session-cli`` command. The runner gives
  its path to the SDK as ``cli_path``. It sends its arguments, its working
  directory, and its environment (without the empty values) to the launcher
  on the Unix socket ``ASSIGNMENTS_SESSION_SOCKET``, and passes its stdin,
  stdout, and stderr with the request. It sends SIGTERM, SIGINT, and SIGHUP
  on to the session, and exits with the exit code of the session.
- ``serve`` is the launcher. For each request, it starts the real CLI
  (``ASSIGNMENTS_SESSION_CLI``, default ``claude``) as its own user, in a new
  process group, with umask 007, on the three passed file descriptors. The
  working directory, ``HOME``, and ``CLAUDE_CONFIG_DIR`` must be in one
  assignment directory ``/work/<id>/``. The launcher removes the names in
  ``DENIED_ENV`` and every ``JOSHUA_TOKEN_`` name from the environment. When
  the wrapper goes, the launcher stops the session. When the session ends,
  the launcher gives the group write access to each file and directory of
  the session in the assignment directory (``share_tree``), because the CLI
  makes some of them with the mode 700 or 600, and the manager must be able
  to remove them.

Neither half logs an argument or an environment value, because the arguments
hold the MCP configuration of the session, and the environment holds its
token.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

SOCKET_ENV = "ASSIGNMENTS_SESSION_SOCKET"
CLI_ENV = "ASSIGNMENTS_SESSION_CLI"
DEFAULT_SOCKET = "/run/joshua-session/launcher.sock"
DEFAULT_CLI = "claude"
WORK_ROOT = Path("/work")
WRAPPER = "joshua-session-cli"
# The secrets of the manager. The runner gives them an empty value, the
# wrapper drops the empty values, and the launcher drops these names again.
DENIED_ENV = frozenset({"DATABASE_URL", "ASSIGNMENTS_TOKENS", "ADDON_TOKEN", SOCKET_ENV})
DENIED_PREFIX = "JOSHUA_TOKEN_"
FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
SESSION_UMASK = 0o007
MAX_REQUEST = 4 * 1024 * 1024
STOP_GRACE_S = 10.0
# The exit codes of the wrapper when the session did not start.
EXIT_REFUSED = 126
EXIT_NOT_STARTED = 127
REPLY_ERRORS = {
    "refused": "the session launcher refused the request",
    "not_started": "the session launcher could not start the CLI",
}


class Refused(ValueError):
    """A request that the launcher does not run. The message names no value."""


# --- the wrapper ---------------------------------------------------------------


def wrapper_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment that the wrapper sends: no empty value, no socket path."""
    return {
        name: value
        for name, value in environ.items()
        if value and name not in DENIED_ENV and not name.startswith(DENIED_PREFIX)
    }


def build_request(args: Sequence[str], environ: Mapping[str, str], cwd: str) -> bytes:
    """The request line of the wrapper: the arguments after the program, the cwd, the env."""
    body = {"op": "run", "argv": list(args), "cwd": cwd, "env": wrapper_env(environ)}
    return json.dumps(body, separators=(",", ":")).encode() + b"\n"


def _read_line(conn: socket.socket, limit: int = MAX_REQUEST) -> bytes:
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise Refused("the request is too long")
    return data.split(b"\n", 1)[0]


def wrapper_main(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> int:
    """Run the session through the launcher and return its exit code."""
    argv = list(sys.argv if argv is None else argv)
    environ = os.environ if environ is None else environ
    path = environ.get(SOCKET_ENV) or DEFAULT_SOCKET
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(path)
    except OSError as exc:
        print(f"{WRAPPER}: cannot reach the session launcher ({exc.strerror})", file=sys.stderr)
        return EXIT_NOT_STARTED
    socket.send_fds(conn, [build_request(argv[1:], environ, os.getcwd())], [0, 1, 2])
    # Only the session keeps the pipes of the SDK, so the SDK sees the end of
    # the output when the session stops.
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)
    os.dup2(null, 1)
    os.close(null)

    def forward(signum: int, _frame: Any) -> None:
        with contextlib.suppress(OSError):
            conn.sendall(json.dumps({"signal": signum}).encode() + b"\n")

    for signum in FORWARDED_SIGNALS:
        signal.signal(signum, forward)
    try:
        reply = json.loads(_read_line(conn) or b"{}")
    except (OSError, ValueError, Refused):
        reply = {}
    reply = reply if isinstance(reply, dict) else {}
    code = reply.get("exit")
    if reply.get("error") in REPLY_ERRORS:
        print(f"{WRAPPER}: {REPLY_ERRORS[reply['error']]}", file=sys.stderr)
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    print(f"{WRAPPER}: the session launcher gave no exit code", file=sys.stderr)
    return 1


# --- the launcher --------------------------------------------------------------


def _assignment_dir(path: str, work_root: Path) -> Path:
    """The ``/work/<id>`` that holds ``path``. Raises Refused when there is none."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise Refused("a path is not absolute")
    resolved = Path(path).resolve()
    root = work_root.resolve()
    try:
        parts = resolved.relative_to(root).parts
    except ValueError:
        raise Refused("a path is not in the work directory") from None
    if not parts or not parts[0].isdigit():
        raise Refused("a path is not in the directory of an assignment")
    return root / parts[0]


def check_request(
    request: Any, work_root: Path = WORK_ROOT
) -> tuple[list[str], dict[str, str], str]:
    """The arguments, the environment, and the cwd of a valid request. Raises Refused."""
    if not isinstance(request, dict) or request.get("op") != "run":
        raise Refused("the request is not a run")
    args, env, cwd = request.get("argv"), request.get("env"), request.get("cwd")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise Refused("argv must be a list of strings")
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise Refused("env must be a map of strings")
    home = _assignment_dir(cwd, work_root)
    for name in ("HOME", "CLAUDE_CONFIG_DIR"):
        if _assignment_dir(env.get(name, ""), work_root) != home:
            raise Refused(f"{name} must be in the directory of the assignment")
    return list(args), wrapper_env(env), cwd


def share_tree(root: Path) -> int:
    """Give the group read and write access to each entry under ``root`` that this user owns.

    A symbolic link stays as it is. Returns the number of entries changed.
    """
    changed = 0
    uid = os.geteuid()
    stack = [root]
    while stack:
        path = stack.pop()
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        is_dir = stat.S_ISDIR(st.st_mode)
        if st.st_uid == uid:
            want = st.st_mode | (0o2770 if is_dir else 0o660)
            if stat.S_IMODE(st.st_mode) != stat.S_IMODE(want):
                with contextlib.suppress(OSError):
                    os.chmod(path, stat.S_IMODE(want))
                    changed += 1
        if is_dir:
            with contextlib.suppress(OSError):
                stack.extend(path / name for name in os.listdir(path))
    return changed


def _signal_group(pid: int, signum: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signum)


def _reply(conn: socket.socket, code: int, error: str | None = None) -> None:
    body: dict[str, Any] = {"exit": code}
    if error:
        body["error"] = error
    with contextlib.suppress(OSError):
        conn.sendall(json.dumps(body).encode() + b"\n")


def _exit_code(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def handle(conn: socket.socket, cli: str, work_root: Path, logger: Any = None) -> int:
    """Serve one wrapper on ``conn``: start the session, wait for it, reply. Returns the code."""
    fds: list[int] = []
    try:
        data, fds, _flags, _addr = socket.recv_fds(conn, 65536, 3)
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_REQUEST:
                raise Refused("the request is too long")
        if len(fds) != 3:
            raise Refused("the request must pass stdin, stdout, and stderr")
        args, env, cwd = check_request(json.loads(data.split(b"\n", 1)[0]), work_root)
    except (Refused, ValueError) as exc:
        if logger is not None:
            logger.warning({"message": "a session request was refused", "reason": str(exc)})
        for fd in fds:
            os.close(fd)
        _reply(conn, EXIT_REFUSED, "refused")
        return EXIT_REFUSED

    try:
        proc = subprocess.Popen(
            [cli, *args],
            stdin=fds[0],
            stdout=fds[1],
            stderr=fds[2],
            cwd=cwd,
            env=env,
            start_new_session=True,
            umask=SESSION_UMASK,
        )
    except OSError as exc:
        if logger is not None:
            logger.error({"message": "the session did not start", "error": type(exc).__name__})
        _reply(conn, EXIT_NOT_STARTED, "not_started")
        return EXIT_NOT_STARTED
    finally:
        for fd in fds:
            os.close(fd)

    def watch() -> None:
        """Send the signals of the wrapper on; stop the session when the wrapper goes."""
        buffer = b""
        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                chunk = b""
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                with contextlib.suppress(ValueError, TypeError):
                    signum = int(json.loads(line).get("signal"))
                    if signum in FORWARDED_SIGNALS:
                        _signal_group(proc.pid, signum)
        if proc.poll() is None:
            _signal_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                _signal_group(proc.pid, signal.SIGKILL)

    threading.Thread(target=watch, daemon=True).start()
    code = _exit_code(proc.wait())
    # A command that the session left in the background stops with it.
    _signal_group(proc.pid, signal.SIGKILL)
    share_tree(_assignment_dir(cwd, work_root))
    _reply(conn, code)
    if logger is not None:
        logger.info({"message": "session ended", "exit": code})
    return code


def listen(path: str) -> socket.socket:
    """Bind the launcher socket at ``path``, readable and writable by the group."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, 0o660)
    server.listen(16)
    return server


def serve(server: socket.socket, cli: str, work_root: Path, logger: Any = None) -> None:
    """Serve each connection on ``server`` in its own thread, until the socket closes."""
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            return

        def run(conn: socket.socket = conn) -> None:
            with conn:
                handle(conn, cli, work_root, logger)

        threading.Thread(target=run, daemon=True).start()


def main(argv: Sequence[str] | None = None) -> None:
    """``python -m joshua_assignments.launcher``: serve the launcher socket."""
    from joshua_shared import log

    parser = argparse.ArgumentParser(prog="python -m joshua_assignments.launcher")
    parser.add_argument("--socket", default=os.environ.get(SOCKET_ENV) or DEFAULT_SOCKET)
    parser.add_argument("--cli", default=os.environ.get(CLI_ENV) or DEFAULT_CLI)
    parser.add_argument("--work-root", default=str(WORK_ROOT))
    opts = parser.parse_args(argv)
    logger = log.configure_from_env("joshua-assignments-session")
    if os.geteuid() == 0:
        print("the session launcher must not run as root", file=sys.stderr)
        raise SystemExit(1)
    cli = shutil.which(opts.cli)
    if cli is None:
        print(f"the session CLI {opts.cli!r} is not on PATH", file=sys.stderr)
        raise SystemExit(1)
    server = listen(opts.socket)
    logger.info({"message": "session launcher ready", "uid": os.geteuid()})
    serve(server, cli, Path(opts.work_root), logger)


if __name__ == "__main__":
    main()
