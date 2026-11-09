"""The git proxy: a read-only git server for the sessions, on localhost.

A session clones over https, but its git never holds a git token. The
manager listens on ``127.0.0.1`` on a free port, in its own process. The
git config of the session (``$HOME/.gitconfig``, see ``git_config``) points
each ``https://`` git URL at this listener and adds one header with a
secret for the assignment:

    [url "http://127.0.0.1:<port>/"]
        insteadOf = https://
    [http "http://127.0.0.1:<port>/"]
        extraHeader = Authorization: Bearer <secret>

The runner makes a new random secret each time a session starts, and
forgets it when the session stops. The secret is not a git token: it opens
only this listener, only for the repositories of that assignment, and only
to read.

For each request, the proxy:

1. finds the assignment of the secret. A missing or wrong secret gets 403;
2. reads the repository from the path (``/<host>/<group>/<name>.git/...``),
   and refuses it unless it is in ``repos`` of the spec or in
   ``granted_repos`` (``grant_repo``), and matches ``assignments.repos`` now.
   A refused repository gets no credential;
3. refuses a push: ``git-receive-pack`` gets 403. The proxy forwards only
   the two requests of a fetch, ``GET info/refs?service=git-upload-pack``
   and ``POST git-upload-pack``. Every other request gets 403;
4. reads the token of the host from the variable that
   ``assignments.platforms.<host>.token_env`` names, puts it on the request
   as HTTP Basic, and sends the request to ``https://<host>/...``. A host
   with no platform entry gets the request with no credential.

The token stays in the manager. It is in no variable, file, or command line
of the session, and in no log line. A refusal is 403 and never 401, so git
does not ask for a password.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import os
import secrets
import socket
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from joshua_assignments.repos import RepoError, in_scope, normalize_repo, repo_allowed
from joshua_assignments.store import Store

logger = get_logger("joshua_assignments.git_proxy")

LISTEN_HOST = "127.0.0.1"
UPLOAD_PACK = "git-upload-pack"
RECEIVE_PACK = "git-receive-pack"
INFO_REFS = "info/refs"
# The user name of the Basic credential, for each kind of host.
CREDENTIAL_USERNAMES = {"github": "x-access-token", "gitlab": "oauth2", "git": "git"}
REQUEST_HEADERS = ("content-type", "accept", "content-encoding", "git-protocol", "user-agent")
RESPONSE_HEADERS = ("content-type", "cache-control", "expires", "pragma")
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 600.0
PUSH_REFUSED = (
    "This container never pushes. Give the change to the developer addon, "
    "or name it as a follow-up.\n"
)


def _refuse(text: str) -> Response:
    return PlainTextResponse(text, status_code=403)


def split_path(path: str) -> tuple[str, str] | None:
    """Split ``/<host>/<repo path>/<suffix>`` into the repository and the suffix.

    The suffix is ``info/refs``, ``git-upload-pack``, or ``git-receive-pack``.
    Returns None for any other path.
    """
    text = path.strip("/")
    for suffix in (INFO_REFS, UPLOAD_PACK, RECEIVE_PACK):
        if text.endswith("/" + suffix):
            return text[: -len(suffix) - 1], suffix
    return None


def git_config(port: int, secret: str, hosts: Iterable[str] = ()) -> str:
    """The ``.gitconfig`` of a session: every git URL goes to the proxy.

    ``hosts`` are the git hosts of the assignment. Their ssh forms go to the
    proxy too, so that a URL such as ``git@host:group/name`` also works. The
    file holds the secret of the assignment and never a git token.
    """
    base = f"http://{LISTEN_HOST}:{port}/"
    lines = [f'[url "{base}"]', "\tinsteadOf = https://"]
    for host in sorted(set(hosts)):
        lines.append(f'[url "{base}{host}/"]')
        lines.append(f"\tinsteadOf = git@{host}:")
        lines.append(f"\tinsteadOf = ssh://git@{host}/")
    lines += [
        f'[http "{base}"]',
        f"\textraHeader = Authorization: Bearer {secret}",
        "[credential]",
        "\thelper =",
        "[init]",
        "\tdefaultBranch = main",
    ]
    return "\n".join(lines) + "\n"


def write_git_config(home: Path, text: str) -> Path:
    """Write ``text`` to ``home/.gitconfig``, readable by its owner only."""
    path = home / ".gitconfig"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    path.chmod(0o600)
    return path


class _QuietServer(uvicorn.Server):
    """A uvicorn server that leaves the signals to the main server of the process."""

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield


class GitProxy:
    """The read-only git server of the sessions. See the module docstring."""

    def __init__(
        self,
        store: Store,
        load_config: Callable[[], JoshuaConfig],
        *,
        environ: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.load_config = load_config
        self.environ = os.environ if environ is None else environ
        self.transport = transport
        self.port: int | None = None
        # The secret of each running session, to the id of its assignment.
        self._grants: dict[str, int] = {}
        self._server: _QuietServer | None = None
        self._task: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None
        self.app = Starlette(routes=[Route("/{path:path}", self._handle, methods=["GET", "POST"])])

    # --- the secrets ------------------------------------------------------

    def grant(self, assignment_id: int) -> str:
        """A new secret for the session of ``assignment_id``. It replaces an older one."""
        self.revoke(assignment_id)
        secret = secrets.token_urlsafe(32)
        self._grants[secret] = int(assignment_id)
        return secret

    def revoke(self, assignment_id: int) -> None:
        """Forget every secret of ``assignment_id``."""
        for secret, owner in list(self._grants.items()):
            if owner == assignment_id:
                del self._grants[secret]

    def owner(self, authorization: str | None) -> int | None:
        """The assignment of the secret in ``authorization``, or None.

        Every secret is compared with ``hmac.compare_digest``, with no early exit.
        """
        raw = authorization or ""
        provided = raw[len("Bearer ") :] if raw.startswith("Bearer ") else ""
        found = None
        for secret, owner in self._grants.items():
            if provided and hmac.compare_digest(provided.encode(), secret.encode()):
                found = owner
        return found

    # --- the listener -----------------------------------------------------

    async def start(self) -> int:
        """Listen on ``127.0.0.1`` on a free port. Returns the port."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LISTEN_HOST, 0))
        self.port = int(sock.getsockname()[1])
        config = uvicorn.Config(
            self.app, lifespan="off", log_config=None, access_log=False, server_header=False
        )
        self._server = _QuietServer(config)
        self._client = httpx.AsyncClient(
            transport=self.transport,
            follow_redirects=False,
            timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        )
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        for _ in range(500):
            if self._server.started or self._task.done():
                break
            await asyncio.sleep(0.01)
        if not self._server.started:
            raise RuntimeError("the git proxy did not start")
        logger.info({"message": "git proxy listening", "port": self.port})
        return self.port

    async def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task
        if self._client is not None:
            await self._client.aclose()
        self._server = self._task = self._client = None

    # --- one request ------------------------------------------------------

    async def _allowed_repo(self, assignment_id: int, repo: str) -> bool:
        row = await self.store.get(assignment_id)
        if row is None or row["status"] != "running":
            return False
        return repo in in_scope(row) and repo_allowed(repo, self.load_config().assignments.repos)

    def _credential(self, host: str) -> tuple[bool, str | None]:
        """``(True, header)`` for a host, or ``(False, None)`` when its token is missing.

        A host with no platform entry gets ``(True, None)``: no credential.
        """
        platform = self.load_config().assignments.platforms.get(host)
        if platform is None:
            return True, None
        token = (self.environ.get(platform.token_env) or "").strip()
        if not token:
            return False, None
        user = CREDENTIAL_USERNAMES.get(platform.kind, "git")
        pair = base64.b64encode(f"{user}:{token}".encode()).decode("ascii")
        return True, f"Basic {pair}"

    async def _handle(self, request: Request) -> Response:
        assignment_id = self.owner(request.headers.get("authorization"))
        if assignment_id is None:
            logger.warning({"message": "git proxy refused: missing or wrong secret"})
            return _refuse("Missing or wrong assignment secret.\n")
        parts = split_path(request.url.path)
        if parts is None:
            logger.warning({"message": "git proxy refused: not a git request", "id": assignment_id})
            return _refuse("Only a git fetch over smart HTTP is allowed.\n")
        raw_repo, suffix = parts
        service = request.query_params.get("service")
        if suffix == RECEIVE_PACK or service == RECEIVE_PACK:
            logger.warning({"message": "git proxy refused a push", "assignment_id": assignment_id})
            return _refuse(PUSH_REFUSED)
        fetch = (suffix == INFO_REFS and request.method == "GET" and service == UPLOAD_PACK) or (
            suffix == UPLOAD_PACK and request.method == "POST"
        )
        if not fetch:
            return _refuse("Only a git fetch over smart HTTP is allowed.\n")
        try:
            repo = normalize_repo(raw_repo)
        except RepoError:
            return _refuse("The path does not name a repository.\n")
        if not await self._allowed_repo(assignment_id, repo):
            logger.warning(
                {"message": "git proxy refused a repository", "assignment_id": assignment_id}
            )
            return _refuse(
                "This repository is not in the repos of the assignment and assignments.repos. "
                "Ask Joshua for it with request_resource.\n"
            )
        host = repo.split("/", 1)[0]
        ok, credential = self._credential(host)
        if not ok:
            logger.warning({"message": "git proxy: the token of the host is not set", "host": host})
            return _refuse("The manager has no token for this host.\n")
        return await self._forward(request, repo, suffix, credential, assignment_id)

    async def _forward(
        self,
        request: Request,
        repo: str,
        suffix: str,
        credential: str | None,
        assignment_id: int,
    ) -> Response:
        assert self._client is not None
        host, _, path = repo.partition("/")
        url = f"https://{host}/{path}.git/{suffix}"
        headers = {k: v for k, v in request.headers.items() if k.lower() in REQUEST_HEADERS}
        if credential is not None:
            headers["Authorization"] = credential
        params = dict(request.query_params)

        async def body() -> AsyncIterator[bytes]:
            async for chunk in request.stream():
                yield chunk

        upstream = self._client.build_request(
            request.method,
            url,
            params=params,
            headers=headers,
            content=body() if request.method == "POST" else None,
        )
        try:
            reply = await self._client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            logger.warning(
                {
                    "message": "git proxy: the host did not answer",
                    "host": host,
                    "error": type(exc).__name__,
                }
            )
            return PlainTextResponse("The git host did not answer.\n", status_code=502)
        logger.info(
            {
                "message": "git proxy request",
                "assignment_id": assignment_id,
                "repo": repo,
                "service": suffix,
                "status": reply.status_code,
            }
        )
        if reply.status_code != 200:
            await reply.aclose()
            # 401 would make git ask for a password, and a redirect would send
            # git to the host with no proxy.
            status = {401: 403, 403: 403, 404: 404}.get(reply.status_code, 502)
            return PlainTextResponse(
                f"The git host answered {reply.status_code}.\n", status_code=status
            )
        out_headers: dict[str, Any] = {
            k: v for k, v in reply.headers.items() if k.lower() in RESPONSE_HEADERS
        }

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in reply.aiter_bytes():
                    yield chunk
            finally:
                await reply.aclose()

        return StreamingResponse(stream(), status_code=200, headers=out_headers)
