"""The snapshot route: ``POST /v1/snapshots/{name}``.

A snapshot is one fixed MCP tool call in ``memory.snapshots``. Core calls this
route on the cadence of each snapshot and puts the output in its memory index.
The body is empty: the gateway reads the server, the tool, the arguments, and
the scope from its own copy of the config. So the config is the allowlist, and
no caller can ask for another tool or other arguments.

Only ``core`` may call the route. The gateway refuses, and logs, a call when:

- the name is not a configured gateway snapshot (404);
- the person of the scope may not use the server (403);
- the tool filter of the server hides the tool, or the upstream does not
  declare it (403);
- the ``class`` map of the server marks the tool ``write-local`` or ``act``
  (403);
- the upstream declares the tool with ``destructiveHint: true`` and not
  ``readOnlyHint: true`` (403).

Otherwise the gateway calls the tool once, through the upstream instance of the
scope's person, with a timeout. The call goes in the call log like any tool
call, with no content.
"""

from __future__ import annotations

import asyncio
import time
from fnmatch import fnmatch
from typing import Any

from joshua_shared import config
from joshua_shared.config import Snapshot
from joshua_shared.contracts import SnapshotResponse
from joshua_shared.fleet_auth import authenticate, load_fleet_tokens
from joshua_shared.log import get_logger
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway.catalog import ServerSpec, instance_name
from joshua_gateway.observability import (
    CALL_LOG,
    CALLERS,
    conversation_ctx,
    identity_ctx,
    now_iso,
    person_ctx,
    role_ctx,
)
from joshua_gateway.upstream import Upstream

logger = get_logger("gateway.snapshots")

# Only core runs snapshots.
SNAPSHOT_CALLERS = frozenset({"core"})

# How long one snapshot call, the tool list and the call together, may take.
SNAPSHOT_TIMEOUT_S = 30.0

# The tool classes that change something. A snapshot must only read.
_REFUSED_CLASSES = ("write-local", "act")


class SnapshotRefused(Exception):
    """The gateway refuses this snapshot call. ``status`` is the HTTP code."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def configured_class(spec: ServerSpec, tool: str) -> str | None:
    """The class that the ``class`` map of ``spec`` gives ``tool``, or None.

    An exact name wins over a pattern. Of two patterns, the first in the map
    wins.
    """
    if tool in spec.tool_classes:
        return spec.tool_classes[tool]
    for pattern, klass in spec.tool_classes.items():
        if fnmatch(tool, pattern):
            return klass
    return None


def is_destructive(tool: Any) -> bool:
    """True when the MCP annotations of ``tool`` say it destroys and do not
    say it only reads."""
    annotations = getattr(tool, "annotations", None)
    if annotations is None:
        return False
    return annotations.destructive_hint is True and annotations.read_only_hint is not True


def join_text(result: Any) -> str:
    """The text parts of a tool result, joined with a newline."""
    parts = [
        getattr(part, "text", "")
        for part in getattr(result, "content", None) or []
        if getattr(part, "type", None) == "text"
    ]
    return "\n".join(parts)


def find_snapshot(name: str) -> Snapshot | None:
    """The gateway snapshot ``name`` from the current config, or None.

    A snapshot on core's in-process scheduling server is not a gateway
    snapshot, so it is None here.
    """
    for snap in config.load().memory.snapshots:
        if snap.name == name and not snap.in_core:
            return snap
    return None


def check_policy(spec: ServerSpec, snap: Snapshot) -> None:
    """Raise ``SnapshotRefused`` when the config alone refuses ``snap``."""
    person = snap.person_id
    if not spec.permits_person(person):
        raise SnapshotRefused(403, "person_not_allowed")
    if not spec.tool_filter.permits(snap.tool):
        raise SnapshotRefused(403, "tool_not_exposed")
    klass = configured_class(spec, snap.tool)
    if klass in _REFUSED_CLASSES:
        raise SnapshotRefused(403, f"tool_class_{klass}")


def resolve_upstream(app_state: Any, spec: ServerSpec, person: str | None) -> Upstream | None:
    """The upstream instance that serves ``person`` on ``spec``, as the proxy
    routes it: one per person for an identity server, else the shared one."""
    upstreams: dict[str, Upstream] = getattr(app_state, "upstreams", {})
    if spec.has_identities:
        if person is None:
            return None
        return upstreams.get(instance_name(spec.name, person))
    return upstreams.get(spec.name)


async def call_snapshot(up: Upstream, snap: Snapshot) -> Any:
    """List the tools, check the declared tool, then call it once.

    Raises ``SnapshotRefused`` when the upstream does not declare the tool or
    declares it destructive.
    """
    listed = await up.forward(lambda session: session.list_tools(), repeatable=True)
    declared = next((t for t in listed.tools if t.name == snap.tool), None)
    if declared is None:
        raise SnapshotRefused(403, "tool_not_exposed")
    if is_destructive(declared):
        raise SnapshotRefused(403, "tool_destructive")
    return await up.forward(
        lambda session: session.call_tool(snap.tool, dict(snap.args)), repeatable=False
    )


def _record(
    identity: str,
    snap: Snapshot | None,
    server: str | None,
    status: str,
    error: str | None,
    duration_ms: float = 0.0,
) -> None:
    """Write one call log entry and one log line. No content."""
    entry = {
        "ts": now_iso(),
        "identity": identity,
        "person": snap.person_id if snap else None,
        "conversation": None,
        "server": server,
        "tool": snap.tool if snap else None,
        "snapshot": snap.name if snap else None,
        "duration_ms": round(duration_ms, 1),
        "status": status,
        "error": error,
    }
    CALL_LOG.record(entry)
    if status == "denied":
        logger.warning({"message": "snapshot refused", **entry})
    else:
        CALLERS.record_call(identity)
        logger.info({"message": "tool call", **entry})


def _authorize(request: Request) -> tuple[JSONResponse | None, str]:
    status, identity = authenticate(
        request.headers.get("authorization"), load_fleet_tokens(), allowed=SNAPSHOT_CALLERS
    )
    if status == 401:
        return JSONResponse({"error": "unauthorized"}, status_code=401), ""
    if status == 403:
        return (
            JSONResponse({"error": f"caller {identity} not allowed on snapshots"}, status_code=403),
            identity or "",
        )
    return None, identity or ""


async def snapshot_route(request: Request) -> JSONResponse:
    """Run one configured snapshot and answer ``SnapshotResponse``."""
    denied, identity = _authorize(request)
    if denied is not None:
        return denied
    name = request.path_params["name"]
    snap = find_snapshot(name)
    if snap is None:
        _record(identity, None, None, "denied", "unknown_snapshot")
        return JSONResponse({"error": "unknown snapshot"}, status_code=404)
    specs: dict[str, ServerSpec] = getattr(request.app.state, "specs", {})
    spec = specs.get(snap.server)
    if spec is None:
        _record(identity, snap, snap.server, "denied", "unknown_server")
        return JSONResponse({"error": "unknown server"}, status_code=404)
    try:
        check_policy(spec, snap)
    except SnapshotRefused as exc:
        _record(identity, snap, snap.server, "denied", exc.reason)
        return JSONResponse({"error": exc.reason}, status_code=exc.status)

    up = resolve_upstream(request.app.state, spec, snap.person_id)
    if up is None or not up.serving:
        _record(identity, snap, snap.server, "error", "upstream_unavailable")
        return JSONResponse({"error": f"upstream {snap.server} unavailable"}, status_code=503)

    tokens = (
        identity_ctx.set(identity),
        person_ctx.set(snap.person_id),
        conversation_ctx.set(None),
        role_ctx.set(None),
    )
    start = time.perf_counter()
    try:
        result = await asyncio.wait_for(call_snapshot(up, snap), SNAPSHOT_TIMEOUT_S)
    except SnapshotRefused as exc:
        _record(identity, snap, snap.server, "denied", exc.reason)
        return JSONResponse({"error": exc.reason}, status_code=exc.status)
    except TimeoutError:
        elapsed = (time.perf_counter() - start) * 1000
        _record(identity, snap, snap.server, "error", "timeout", elapsed)
        return JSONResponse({"error": "timeout"}, status_code=504)
    except Exception as exc:  # noqa: BLE001 — any upstream fault is one 502
        elapsed = (time.perf_counter() - start) * 1000
        _record(identity, snap, snap.server, "error", type(exc).__name__, elapsed)
        return JSONResponse({"error": f"upstream {snap.server} failed the call"}, status_code=502)
    finally:
        identity_ctx.reset(tokens[0])
        person_ctx.reset(tokens[1])
        conversation_ctx.reset(tokens[2])
        role_ctx.reset(tokens[3])

    elapsed = (time.perf_counter() - start) * 1000
    is_error = bool(getattr(result, "is_error", False))
    _record(
        identity,
        snap,
        snap.server,
        "error" if is_error else "success",
        "tool error" if is_error else None,
        elapsed,
    )
    body = SnapshotResponse(
        ok=not is_error, text=join_text(result), is_error=is_error, fetched_at=now_iso()
    )
    return JSONResponse(body.model_dump())
