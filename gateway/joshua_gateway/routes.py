"""ASGI routes for the upstream endpoints.

Each upstream is served at both ``/<name>`` and ``/<name>/…`` with no
trailing-slash redirect, so every MCP tool call is a single round trip. The ASGI
app authenticates the request with ``joshua_shared.fleet_auth`` (the matched
``JOSHUA_TOKEN_*`` key is the caller identity), checks the caller is allowed on
MCP routes, attributes the request to a person, applies the per-server person
gate, binds the person to the session, then forwards to the upstream session
manager (503 while down).

The person comes from ``X-Joshua-Person`` and the role from ``X-Joshua-Role``,
both trusted only from ``core``. The role is what the files server acts on. A server
with
``allow: all`` admits any person; a scoped server admits only its named people
and denies a request with no person. A denied request is 403 before any upstream
contact and is written to the call log. A later request that changes the person
or conversation of a live session is 409.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from joshua_shared import config
from joshua_shared.fleet_auth import identity_from_scope, load_fleet_tokens
from starlette.routing import Match, NoMatchFound

from joshua_gateway import person as person_policy
from joshua_gateway.catalog import ServerSpec
from joshua_gateway.observability import (
    CALL_LOG,
    CALLERS,
    SESSIONS,
    conversation_ctx,
    identity_ctx,
    now_iso,
    person_ctx,
    role_ctx,
)
from joshua_gateway.upstream import Upstream

Send = Callable[[dict[str, Any]], Awaitable[None]]

_SESSION_HEADER = b"mcp-session-id"


async def _reject(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _headers(scope) -> dict[str, str]:
    """Lower-cased header map from the ASGI scope."""
    return {
        name.decode("latin-1").lower(): value.decode("latin-1")
        for name, value in scope.get("headers", [])
    }


def _session_key(headers: dict[str, str], conversation: str | None) -> str | None:
    """The connection key for session binding: the transport session id, else the
    conversation id, else None (no binding)."""
    return headers.get("mcp-session-id") or conversation


def _strip_session_header(scope):
    """Return a copy of the scope without ``mcp-session-id``; the stateless session
    manager does not use it and must not reject a gateway-issued value."""
    stripped = [pair for pair in scope.get("headers", []) if pair[0] != _SESSION_HEADER]
    return {**scope, "headers": stripped}


def _record_denied(
    server: str,
    identity: str,
    person: str | None,
    conversation: str | None,
    error: str = "person_not_allowed",
) -> None:
    """Log a person-denied request in the call log (no upstream call happened)."""
    CALL_LOG.record(
        {
            "ts": now_iso(),
            "identity": identity,
            "person": person,
            "conversation": conversation,
            "server": server,
            "tool": None,
            "duration_ms": 0.0,
            "status": "denied",
            "error": error,
        }
    )


async def _authenticate(scope, allowed: frozenset[str], name: str, send: Send):
    """Authenticate the caller and attribute the request to a person.

    Returns ``(identity, attribution)`` on success, or None after sending a 401
    (no/invalid bearer), 403 (caller not allowed on MCP routes), or 400 (a bad
    person header). ``name`` names the route in the 403 body.
    """
    identity = identity_from_scope(scope, load_fleet_tokens())
    if identity is None:
        await _reject(send, 401, b"unauthorized")
        return None
    if identity not in allowed:
        await _reject(send, 403, f"caller {identity} not allowed on {name}".encode())
        return None
    headers = _headers(scope)
    try:
        attr = person_policy.resolve(
            identity,
            headers.get("x-joshua-person"),
            headers.get("x-joshua-conversation"),
            _known_persons(),
            headers.get("x-joshua-role"),
        )
    except person_policy.PersonError as exc:
        await _reject(send, 400, exc.message.encode())
        return None
    return identity, attr


async def _forward(up: Upstream, identity: str, attr, scope, receive, send) -> None:
    """Bind the request context and forward it to the upstream session manager.

    The session is bound and conflict-checked by ``up.name`` (the instance), so an
    identity server binds each person's session to that person's own instance.
    """
    headers = _headers(scope)
    key = _session_key(headers, attr.conversation)
    if key is not None:
        conflict = SESSIONS.check(up.name, key, attr.person, attr.conversation)
        if conflict is not None:
            SESSIONS.close(up.name, key)
            await _reject(send, 409, f"session {conflict}".encode())
            return

    CALLERS.touch(identity, up.label)
    tokens = (
        identity_ctx.set(identity),
        person_ctx.set(attr.person),
        conversation_ctx.set(attr.conversation),
        role_ctx.set(attr.role),
    )
    try:
        mgr = up.mgr
        if mgr is None:
            await _reject(send, 503, f"upstream {up.label} unavailable".encode())
            return
        await mgr.handle_request(_strip_session_header(scope), receive, send)
    finally:
        identity_ctx.reset(tokens[0])
        person_ctx.reset(tokens[1])
        conversation_ctx.reset(tokens[2])
        role_ctx.reset(tokens[3])


def make_asgi(up: Upstream, allowed: frozenset[str]):
    """ASGI app for one shared upstream endpoint (a server without identities).

    ``allowed`` is the set of fleet callers permitted on MCP routes (``core``).
    A missing or invalid bearer is 401; a caller outside
    ``allowed`` is 403; a person the server does not admit is 403; a bad person
    header is 400; a session that changes person or conversation is 409.
    """

    async def asgi(scope, receive, send) -> None:
        result = await _authenticate(scope, allowed, up.label, send)
        if result is None:
            return
        identity, attr = result
        if not up.spec.permits_person(attr.person):
            _record_denied(up.label, identity, attr.person, attr.conversation)
            await _reject(send, 403, f"{up.label} is not open to this request".encode())
            return
        await _forward(up, identity, attr, scope, receive, send)

    return asgi


def make_multi_asgi(
    name: str,
    get_spec: Callable[[], ServerSpec],
    resolve_instance: Callable[[str], Upstream | None],
    allowed: frozenset[str],
):
    """ASGI app for an identity server: route each person to their own instance.

    ``get_spec`` returns the current server spec (for the per-person allow gate)
    and ``resolve_instance`` returns the live upstream instance for a person.
    Both read live state so a reload that adds or removes a person takes effect
    without a route change. A request with no person is 403 ``person_required``;
    a person the server does not admit is 403; otherwise the request forwards to
    that person's instance.
    """

    async def asgi(scope, receive, send) -> None:
        result = await _authenticate(scope, allowed, name, send)
        if result is None:
            return
        identity, attr = result
        if attr.person is None:
            _record_denied(name, identity, attr.person, attr.conversation, "person_required")
            await _reject(send, 403, b"person_required")
            return
        if not get_spec().permits_person(attr.person):
            _record_denied(name, identity, attr.person, attr.conversation)
            await _reject(send, 403, f"{name} is not open to this request".encode())
            return
        up = resolve_instance(attr.person)
        if up is None:
            _record_denied(name, identity, attr.person, attr.conversation)
            await _reject(send, 403, f"{name} is not open to this request".encode())
            return
        await _forward(up, identity, attr, scope, receive, send)

    return asgi


def _known_persons() -> frozenset[str]:
    """Person ids from the current config, for validating a person header."""
    return frozenset(p.id for p in config.load().people)


class UpstreamRoute:
    """Serves one upstream's ASGI app at both ``/<name>`` and ``/<name>/…``.

    Starlette's ``Mount`` only matches ``/<name>/…`` and 307-redirects a bare
    ``/<name>`` (what every MCP client sends), doubling the round trips. This
    route matches both forms directly; the inner ASGI ignores the path.
    """

    def __init__(self, name: str, app):
        self.name = name
        self.prefix = f"/{name}"
        self.app = app

    def matches(self, scope):
        if scope["type"] == "http":
            path = scope.get("path", "")
            # The trailing "/" boundary keeps /tracker from matching /tracker-dev.
            if path == self.prefix or path.startswith(self.prefix + "/"):
                return Match.FULL, {}
        return Match.NONE, {}

    async def handle(self, scope, receive, send):
        await self.app(scope, receive, send)

    def url_path_for(self, name: str, /, **path_params):
        raise NoMatchFound(name, path_params)
