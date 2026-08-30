"""Admin routes: reload, call log, inventory.

Every ``/admin/*`` route requires an identity in ``ADMIN_CALLERS`` (env, default
``laptop,ci``) — checked on every route with no exception. A missing or invalid
bearer is 401; a valid identity outside the allowlist is 403.
"""

from __future__ import annotations

import os
from typing import Any

from joshua_shared import config
from joshua_shared.fleet_auth import (
    DEFAULT_ADMIN_CALLERS,
    authenticate,
    load_fleet_tokens,
    parse_callers,
)
from joshua_shared.log import get_logger
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway.catalog import ServerSpec
from joshua_gateway.observability import CALL_LOG, CALLERS, SESSIONS
from joshua_gateway.upstream import Upstream

logger = get_logger("gateway.admin")


def _authorize(request: Request) -> JSONResponse | None:
    """Return a 401/403 response when the request is not an admin caller, else None."""
    admin_callers = parse_callers(os.environ.get("ADMIN_CALLERS", DEFAULT_ADMIN_CALLERS))
    status, identity = authenticate(
        request.headers.get("authorization"), load_fleet_tokens(), allowed=admin_callers
    )
    if status == 401:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if status == 403:
        return JSONResponse({"error": f"caller {identity} not in ADMIN_CALLERS"}, status_code=403)
    return None


async def admin_reload(request: Request) -> JSONResponse:
    """Re-read joshua.yaml and reconcile the running upstreams.

    A full reload diffs the catalog: new entries start, removed entries stop,
    changed entries restart, unchanged entries keep their sessions. Optional body
    ``{"server": "<name>"}`` restarts one instead. An unknown server name is 404;
    a config that no longer parses is 400."""
    # A late import breaks the main <-> admin import cycle.
    from joshua_gateway.main import reconcile_catalog

    denied = _authorize(request)
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — empty or absent body is fine
        body = {}
    target = (body or {}).get("server")
    try:
        result = await reconcile_catalog(request.app, target)
    except config.ConfigError as exc:
        return JSONResponse({"error": f"config reload failed: {exc}"}, status_code=400)
    except KeyError:
        known = sorted(request.app.state.specs)
        return JSONResponse({"error": f"unknown server: {target}", "known": known}, status_code=404)
    return JSONResponse(result, status_code=200 if not result["failed"] else 502)


async def admin_calls(request: Request) -> JSONResponse:
    """Recent proxied tool calls, newest first. Query params: ``identity`` and
    ``tool`` (case-insensitive substring filters), ``limit`` (default 200)."""
    denied = _authorize(request)
    if denied is not None:
        return denied
    q = request.query_params
    try:
        limit = int(q.get("limit", "200"))
    except (TypeError, ValueError):
        limit = 200
    calls = CALL_LOG.recent(
        identity=q.get("identity") or None, tool=q.get("tool") or None, limit=limit
    )
    return JSONResponse({"calls": calls})


def _server_status(instances: list[Upstream]) -> str:
    """Aggregate an identity server's status from its instances: ``connected``
    only when every instance is connected, ``error`` when any has failed, else
    ``connecting``."""
    statuses = {up.status for up in instances}
    if statuses == {"connected"}:
        return "connected"
    if "error" in statuses:
        return "error"
    return "connecting"


async def admin_inventory(request: Request) -> JSONResponse:
    """Full inventory: every server with its status, tool names, ``allow`` list,
    the persons that currently have a live session, the per-person instances of an
    identity server, and the reserved policy fields (tool classes, url_args), plus
    the identities seen and the servers each has touched."""
    denied = _authorize(request)
    if denied is not None:
        return denied
    specs: dict[str, ServerSpec] = request.app.state.specs
    upstreams: dict[str, Upstream] = request.app.state.upstreams

    servers: dict[str, Any] = {}
    for name, spec in specs.items():
        instances = sorted(
            (up for up in upstreams.values() if up.label == name),
            key=lambda up: up.person or "",
        )
        entry: dict[str, Any] = {
            "label": name,
            "allow": "all" if spec.is_open else sorted(spec.allow_persons),
            "persons": sorted({p for up in instances for p in SESSIONS.live_persons(up.name)}),
            "url_args": spec.url_args,
            "tool_classes": spec.tool_classes,
            "identities": sorted(spec.identities),
            "instances": [
                {"person": up.person, "status": up.status, "tool_count": up.tool_count}
                for up in instances
            ],
        }
        if spec.is_builtin:
            up = instances[0]  # the single in-process instance
            entry.update(
                {
                    "kind": "builtin",
                    "builtin": spec.builtin,
                    "status": up.status,
                    "tools": up.tools,
                    "error": up.error,
                }
            )
        elif spec.has_identities:
            entry.update(
                {
                    "transport": spec.transport,
                    "status": _server_status(instances),
                    "tools": sorted({t for up in instances for t in up.tools}),
                    "error": next((up.error for up in instances if up.error), None),
                }
            )
        else:
            up = instances[0]  # the single shared instance
            entry.update(
                {
                    "transport": spec.transport,
                    "status": up.status,
                    "tools": up.tools,
                    "error": up.error,
                }
            )
        servers[name] = entry
    return JSONResponse({"servers": servers, "identities": CALLERS.snapshot()})
