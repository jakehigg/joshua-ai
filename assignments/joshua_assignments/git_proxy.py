"""The git proxy: a read-only git server for the sessions, in its own container.

A session clones over https, but its git never holds a git token. The proxy
runs in a container of its own, beside the assignments container, and it is
the only part of the deployment that reaches the git hosts: the container of
the session has no route to them, so a shell in a session reaches no git host
at all. The proxy listens on ``0.0.0.0`` on :data:`LISTEN_PORT`, and
``python -m joshua_assignments.git_proxy`` starts it.

The git config of the session (``$HOME/.gitconfig``, see ``git_config``)
points each ``https://`` git URL at the proxy and adds one header with a
secret for the assignment:

    [url "http://joshua-git-proxy:8003/"]
        insteadOf = https://
    [http "http://joshua-git-proxy:8003/"]
        extraHeader = Authorization: Bearer <secret>

The runner makes a new random secret each time a session starts
(``grant_secret``), and forgets it when the session stops
(``revoke_secrets``). The two processes share the grant through the table
``assignments.git_grant``, which holds the SHA-256 of the secret and never
the secret itself. The secret is not a git token: it opens only this
listener, only for the repositories of that assignment, and only to read.

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

The token stays in this container. It is in no variable, file, or command
line of the session, and in no log line. A refusal is 403 and never 401, so
git does not ask for a password.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import secrets
import socket
import sys
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from joshua_shared import config, log
from joshua_shared.config import JoshuaConfig
from joshua_shared.log import get_logger
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from joshua_assignments.repos import RepoError, in_scope, normalize_repo, repo_allowed
from joshua_assignments.store import PgStore, Store

logger = get_logger("joshua_assignments.git_proxy")

# The proxy is a container of its own, so it listens on every address of that
# container. Only the assignments pod may reach it (the network policy of the
# chart, the compose networks), and only with the secret of a running session.
LISTEN_HOST = "0.0.0.0"
# The fixed port of the listener. The session reaches it by the name of its
# service, so the port is part of that address and never a free one.
LISTEN_PORT = 8003
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


def git_config(proxy_url: str, secret: str, hosts: Iterable[str] = ()) -> str:
    """The ``.gitconfig`` of a session: every git URL goes to the proxy.

    ``proxy_url`` is the address of the proxy container, such as
    ``http://joshua-git-proxy:8003``. ``hosts`` are the git hosts of the
    assignment. Their ssh forms go to the proxy too, so that a URL such as
    ``git@host:group/name`` also works. The file holds the secret of the
    assignment and never a git token.
    """
    base = proxy_url.rstrip("/") + "/"
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


def secret_digest(secret: str) -> str:
    """The SHA-256 of a secret, in hex. The store holds this, never the secret."""
    return hashlib.sha256(secret.encode()).hexdigest()


async def grant_secret(store: Store, assignment_id: int) -> str:
    """A new secret for the session of ``assignment_id``. It replaces an older one."""
    secret = secrets.token_urlsafe(32)
    await store.put_git_grant(int(assignment_id), secret_digest(secret))
    return secret


async def revoke_secrets(store: Store, assignment_id: int) -> None:
    """Forget every secret of ``assignment_id``."""
    await store.clear_git_grants(int(assignment_id))


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
        port: int = LISTEN_PORT,
        environ: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.load_config = load_config
        self.environ = os.environ if environ is None else environ
        self.transport = transport
        self.port = port
        self._server: _QuietServer | None = None
        self._task: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None
        self.app = Starlette(
            routes=[
                Route("/healthz", self._healthz, methods=["GET"]),
                Route("/{path:path}", self._handle, methods=["GET", "POST"]),
            ],
            lifespan=self._lifespan,
        )

    # --- the secrets ------------------------------------------------------

    async def owner(self, authorization: str | None) -> int | None:
        """The assignment of the secret in ``authorization``, or None.

        The store holds the SHA-256 of each secret, so the lookup compares a
        digest and never the secret itself.
        """
        raw = authorization or ""
        provided = raw[len("Bearer ") :] if raw.startswith("Bearer ") else ""
        if not provided:
            return None
        return await self.store.git_grant_owner(secret_digest(provided))

    # --- the listener -----------------------------------------------------

    @contextlib.asynccontextmanager
    async def _lifespan(self, app: Starlette) -> AsyncIterator[None]:
        """Open and close the store of the standalone server (see ``main``)."""
        await self.store.open()
        try:
            yield
        finally:
            await self.aclose_client()
            await self.store.close()

    async def _healthz(self, request: Request) -> Response:
        return JSONResponse({"ok": True})

    def client(self) -> httpx.AsyncClient:
        """The client to the git hosts, made at the first request."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self.transport,
                follow_redirects=False,
                timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            )
        return self._client

    async def aclose_client(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        self._client = None

    async def start(self) -> int:
        """Listen on every address of the container, on ``self.port``. Returns the port.

        Port 0 takes a free port, which the tests use.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((LISTEN_HOST, self.port))
        self.port = int(sock.getsockname()[1])
        server_config = uvicorn.Config(
            self.app, lifespan="off", log_config=None, access_log=False, server_header=False
        )
        self._server = _QuietServer(server_config)
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
        await self.aclose_client()
        self._server = self._task = None

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
        assignment_id = await self.owner(request.headers.get("authorization"))
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
        client = self.client()
        host, _, path = repo.partition("/")
        url = f"https://{host}/{path}.git/{suffix}"
        headers = {k: v for k, v in request.headers.items() if k.lower() in REQUEST_HEADERS}
        if credential is not None:
            headers["Authorization"] = credential
        params = dict(request.query_params)

        async def body() -> AsyncIterator[bytes]:
            async for chunk in request.stream():
                yield chunk

        upstream = client.build_request(
            request.method,
            url,
            params=params,
            headers=headers,
            content=body() if request.method == "POST" else None,
        )
        try:
            reply = await client.send(upstream, stream=True)
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


def main() -> None:
    """Run the proxy as its own container: ``python -m joshua_assignments.git_proxy``.

    It needs ``DATABASE_URL``, the same database as the manager, and the read
    token of each git host, under the name that
    ``assignments.platforms.<host>.token_env`` gives. It holds no fleet token
    and no model credential, and it reads no volume of the instance.
    """
    logger = log.configure_from_env("joshua-git-proxy")
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if not cfg.assignments.enabled:
        logger.error(
            {
                "message": "assignments is disabled; set assignments.enabled: true "
                "in joshua.yaml to start the git proxy"
            }
        )
        raise SystemExit(1)
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        print("settings error: DATABASE_URL is required", file=sys.stderr)
        raise SystemExit(1)
    # The manager owns the schema; this process only reads a grant and a row.
    proxy = GitProxy(PgStore(database_url, apply_schema=False), config.load)
    logger.info({"message": "git proxy listening", "port": LISTEN_PORT})
    uvicorn.run(
        proxy.app,
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        log_config=None,
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
