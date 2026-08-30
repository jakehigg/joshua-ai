"""Fleet identity auth for joshua containers.

One shared Secret holds a ``JOSHUA_TOKEN_<NAME>`` for each fleet identity. A
caller sends its own token as ``Authorization: Bearer <token>``. A callee
matches the bearer against the whole map with ``hmac.compare_digest`` — the
matched key, lowercased, IS the caller identity. A missing or invalid bearer is
401. An identity outside an endpoint allowlist is 403.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from fastapi import HTTPException, Request

TOKEN_PREFIX = "JOSHUA_TOKEN_"
DEFAULT_ADMIN_CALLERS = "laptop,ci"


def load_fleet_tokens(env: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Return an identity → token map from ``JOSHUA_TOKEN_*`` env vars.

    Empty values are skipped.
    """
    return {
        key[len(TOKEN_PREFIX) :].lower(): value
        for key, value in env.items()
        if key.startswith(TOKEN_PREFIX) and value
    }


def parse_callers(value: str) -> frozenset[str]:
    """Split a comma list into a lowercase set of identities."""
    return frozenset(part.strip().lower() for part in value.split(",") if part.strip())


def identify_bearer(header_value: str | None, tokens: Mapping[str, str]) -> str | None:
    """Match an ``Authorization`` header against the fleet map.

    Compares against every token with ``hmac.compare_digest`` and no early exit,
    so the match time does not leak which identity matched. Returns the matched
    identity or None.
    """
    if not header_value or not header_value.startswith("Bearer "):
        return None
    presented = header_value[7:]
    matched: str | None = None
    for identity, token in tokens.items():
        if hmac.compare_digest(presented, token):
            matched = identity
    return matched


def authenticate(
    header_value: str | None,
    tokens: Mapping[str, str],
    *,
    allowed: Iterable[str] | None,
) -> tuple[int, str | None]:
    """Run the full inbound check → ``(status_code, identity)``.

    Returns ``(401, None)`` for a missing or invalid bearer, ``(403, identity)``
    when ``allowed`` is given and the identity is not in it, else
    ``(200, identity)``. An empty allowlist admits any authenticated identity.
    """
    identity = identify_bearer(header_value, tokens)
    if identity is None:
        return 401, None
    if allowed is not None:
        allowset = {name.strip().lower() for name in allowed if name and name.strip()}
        if allowset and identity not in allowset:
            return 403, identity
    return 200, identity


def require_identity(
    allowed: Iterable[str] | None = None,
    *,
    admin: bool = False,
) -> Callable[[Request], str]:
    """Build a FastAPI dependency that returns the caller identity or raises.

    Raises ``HTTPException`` 401 for a missing or invalid bearer and 403 for an
    identity outside the allowlist. With ``admin=True`` the allowlist comes from
    the ``ADMIN_CALLERS`` env var (comma list, default ``laptop,ci``) and
    replaces ``allowed``.
    """
    fixed_allowed = list(allowed) if allowed is not None else None

    def dependency(request: Request) -> str:
        if admin:
            effective: Iterable[str] | None = parse_callers(
                os.environ.get("ADMIN_CALLERS", DEFAULT_ADMIN_CALLERS)
            )
        else:
            effective = fixed_allowed
        tokens = load_fleet_tokens()
        status, identity = authenticate(
            request.headers.get("authorization"), tokens, allowed=effective
        )
        if status == 401:
            raise HTTPException(status_code=401, detail="missing or invalid bearer")
        if status == 403:
            raise HTTPException(status_code=403, detail="identity not allowed")
        assert identity is not None
        return identity

    return dependency


def identity_from_scope(scope: Mapping[str, Any], tokens: Mapping[str, str]) -> str | None:
    """Return the caller identity from a raw ASGI ``scope``, or None.

    Reads the ``authorization`` header from ``scope['headers']`` (a list of
    ``(name, value)`` byte pairs) and matches it like ``identify_bearer``. Use
    this on the gateway's raw ASGI routes.
    """
    header_value: str | None = None
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            header_value = value.decode("latin-1")
            break
    return identify_bearer(header_value, tokens)
