"""``/v1/model/{path}``, any method: core's model forwarder.

The assignments container holds no model credential. Its Claude CLI sets
``ANTHROPIC_BASE_URL`` to core's URL and ``/v1/model``, and
``CLAUDE_CODE_OAUTH_TOKEN`` to the container's own fleet token. The CLI then
sends that fleet token as a Claude token. Core checks it, puts the credential
of the inference job ``assignment`` on the call, and sends the call on. The
credential never leaves core.

``inference.forwarder.allowed_callers`` names the fleet identities that may
call (default ``assignments``). An empty list turns the route off, so every
request gets 404. The bearer is compared with the tokens of these identities
only, so a missing token, a wrong token, and the token of any other identity
(``core`` too) get 401.

Core sends a call on only when ``path`` is in ``MODEL_PATHS``, or is
``v1/models/<id>`` with no dot segment. Any other path gets 403. Core answers
``api/hello`` (GET or HEAD) itself with ``{}`` and no bearer: it is the
connectivity probe of the Claude CLI.

The upstream is the server of the ``assignment`` job
(``joshua_shared.inference.resolve_inference``): ``inference.assignment``, else
the global ``inference`` settings, else the Anthropic API with the
subscription token. Core removes ``Authorization``, ``x-api-key``, ``Host``,
and the hop-by-hop headers, then adds the credential of the job. It passes the
other headers on, and the query. With a credential that is not the
subscription token, it also removes the ``oauth-*`` flags from
``anthropic-beta``, because the caller's CLI sets them for its stand-in token.
The body streams in both directions, so a server-sent event stream arrives at
the caller as the server sends it. Core does not change the body, so the
model of a call is the model that the caller asks for.

The forwarder does not write a usage row: the assignments container reports
its own usage with ``POST /v1/usage``. It never logs a token, a header value,
or a body. When the server cannot be reached, the answer is 502 with a fixed
error, and the log names the error type and the server label only, never the
error text.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from joshua_shared.config import JoshuaConfig
from joshua_shared.contracts import MODEL_FORWARDER_PATH
from joshua_shared.fleet_auth import identify_bearer, load_fleet_tokens
from joshua_shared.inference import ResolvedInference
from joshua_shared.log import get_logger
from starlette.background import BackgroundTask

from joshua_core.engine import inference as inference_plan

logger = get_logger("model_forwarder")

FORWARDER_JOB = "assignment"
ANTHROPIC_API = "https://api.anthropic.com"

# The API paths that the Claude CLI calls. Every other path gets 403.
MODEL_PATHS = frozenset({"v1/messages", "v1/messages/count_tokens", "v1/models"})
# v1/models/<model id>: one more path segment, with no dot segment.
_MODEL_ID_PATH = re.compile(r"v1/models/[A-Za-z0-9_-][A-Za-z0-9._-]*")
# The connectivity probe of the Claude CLI. Core answers it.
PROBE_PATH = "api/hello"

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Headers that never go to the server. The credential of the job comes back in
# their place, and the host comes from the server URL.
_DROP_REQUEST = frozenset(
    {
        "authorization",
        "x-api-key",
        "host",
        "connection",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_DROP_RESPONSE = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_BETA_HEADER = "anthropic-beta"
_OAUTH_BETA_PREFIX = "oauth-"

Resolver = Callable[[JoshuaConfig, str], ResolvedInference]


def path_allowed(path: str) -> bool:
    """True when the forwarder sends ``path`` on to the server."""
    if path in MODEL_PATHS:
        return True
    return _MODEL_ID_PATH.fullmatch(path) is not None and ".." not in path


def upstream_base(resolved: ResolvedInference) -> str:
    """The server URL of the job, with no trailing ``/``."""
    return (resolved.env.get("ANTHROPIC_BASE_URL") or ANTHROPIC_API).rstrip("/")


def credential_headers(resolved: ResolvedInference) -> tuple[list[tuple[str, str]], bool] | None:
    """The credential headers of the job, and True when the credential is the
    subscription token.

    Returns None when the job uses the subscription and core has no token.
    A server in ``base_url`` with an empty key gets no credential header.
    """
    env = resolved.env
    if env.get("ANTHROPIC_BASE_URL"):
        key = env.get("ANTHROPIC_AUTH_TOKEN", "")
        return ([("authorization", f"Bearer {key}")] if key else []), False
    if env.get("ANTHROPIC_API_KEY"):
        return [("x-api-key", env["ANTHROPIC_API_KEY"])], False
    token = env.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not token:
        return None
    return [("authorization", f"Bearer {token}")], True


def _without_oauth_beta(value: str) -> str:
    flags = [f.strip() for f in value.split(",")]
    return ",".join(f for f in flags if f and not f.startswith(_OAUTH_BETA_PREFIX))


def request_headers(
    incoming: list[tuple[str, str]], credential: list[tuple[str, str]], *, oauth: bool
) -> list[tuple[str, str]]:
    """The headers that go to the server: ``incoming`` with the caller's
    credential and the hop-by-hop headers removed, then ``credential``."""
    out: list[tuple[str, str]] = []
    for key, value in incoming:
        lower = key.lower()
        if lower in _DROP_REQUEST:
            continue
        if lower == _BETA_HEADER and not oauth:
            value = _without_oauth_beta(value)
            if not value:
                continue
        out.append((key, value))
    out.extend(credential)
    return out


def _error(status: int, reason: str) -> JSONResponse:
    return JSONResponse({"error": reason}, status_code=status)


def _allowed_tokens(callers: list[str]) -> dict[str, str]:
    """The fleet tokens of the identities in ``callers`` only."""
    names = {name.strip().lower() for name in callers}
    return {name: token for name, token in load_fleet_tokens().items() if name in names}


class ModelForwarder:
    """The forwarder route and its HTTP client.

    ``client`` is the HTTP client for the server. Without one, the forwarder
    makes one on first use, and ``aclose`` closes it. ``resolver`` gives the
    job's server and credential; the default is ``engine.inference.resolve``.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._given = client
        self._client = client
        self._resolve: Resolver = resolver or inference_plan.resolve
        self.router = self._build_router()

    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
            )
        return self._client

    async def aclose(self) -> None:
        """Close the client that the forwarder made. A given client stays open."""
        made, self._client = self._client, self._given
        if made is not None and made is not self._given:
            await made.aclose()

    def _build_router(self) -> APIRouter:
        router = APIRouter()
        http = self.http

        @router.api_route(
            f"{MODEL_FORWARDER_PATH}/{{path:path}}", methods=METHODS, include_in_schema=False
        )
        async def forward(request: Request, path: str) -> Response:
            settings: JoshuaConfig = request.app.state.ctx.settings
            callers = settings.inference.forwarder.allowed_callers
            if not callers:
                return _error(404, "not found")
            if path == PROBE_PATH and request.method in ("GET", "HEAD"):
                return JSONResponse({})
            caller = identify_bearer(request.headers.get("authorization"), _allowed_tokens(callers))
            if caller is None:
                logger.warning({"message": "forwarder refused: missing or wrong token"})
                return _error(401, "unauthorized")
            if not path_allowed(path):
                logger.warning(
                    {
                        "message": "forwarder refused a path",
                        "caller": caller,
                        "method": request.method,
                        "api_path": path[:200],
                    }
                )
                return _error(403, "path not allowed")
            resolved = self._resolve(settings, FORWARDER_JOB)
            found = credential_headers(resolved)
            if found is None:
                logger.warning({"message": "forwarder has no model credential", "caller": caller})
                return _error(503, "model credential not configured")
            credential, oauth = found
            url = httpx.URL(f"{upstream_base(resolved)}/{path}")
            if request.url.query:
                url = url.copy_with(query=request.url.query.encode())
            headers = request_headers(list(request.headers.items()), credential, oauth=oauth)
            has_body = "content-length" in request.headers or "transfer-encoding" in request.headers
            upstream_request = http().build_request(
                request.method,
                url,
                headers=headers,
                content=_body(request) if has_body else None,
            )
            try:
                upstream = await http().send(upstream_request, stream=True)
            except httpx.HTTPError as exc:
                logger.warning(
                    {
                        "message": "the model server did not answer",
                        "caller": caller,
                        "endpoint": resolved.endpoint,
                        "error_type": type(exc).__name__,
                    }
                )
                return _error(502, "model server unreachable")
            logger.debug(
                {
                    "message": "forwarded",
                    "caller": caller,
                    "api_path": path,
                    "endpoint": resolved.endpoint,
                    "status": upstream.status_code,
                }
            )
            response_headers = {
                key: value
                for key, value in upstream.headers.items()
                if key.lower() not in _DROP_RESPONSE
            }
            return StreamingResponse(
                upstream.aiter_raw(),
                status_code=upstream.status_code,
                headers=response_headers,
                background=BackgroundTask(upstream.aclose),
            )

        return router


async def _body(request: Request) -> AsyncIterator[bytes]:
    async for chunk in request.stream():
        if chunk:
            yield chunk
