"""MCP gateway: hosts the filtered MCP upstreams over streamable-HTTP.

The gateway holds every upstream credential so a consumer holds only its own fleet
identity token. It serves one streamable-HTTP endpoint per upstream at ``/<name>``,
authenticates every request with ``joshua_shared.fleet_auth`` (auth is always
required), admits only ``core`` on the MCP routes, and enforces
each server's tool filter server-side at both ``tools/list`` and call time.

The catalog is the ``mcp:`` section of joshua.yaml, resolved by
``joshua_gateway.catalog.build_catalog``. Each external upstream is owned by a
supervisor task that reconnects after a transport error and after
``POST /admin/reload``. A builtin server (``kind: builtin``, the files MCP) runs
in-process over an in-memory transport, owned by the same supervisor as an external
upstream.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager

from joshua_shared import config
from joshua_shared.fleet_auth import load_fleet_tokens
from joshua_shared.log import get_logger, install_healthcheck_filter
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from joshua_gateway.admin import (
    admin_calls,
    admin_inventory,
    admin_mcp_install,
    admin_reload,
)
from joshua_gateway.catalog import ServerSpec, build_catalog, instance_name, instance_specs
from joshua_gateway.routes import UpstreamRoute, make_asgi, make_multi_asgi
from joshua_gateway.upstream import Upstream

logger = get_logger("gateway")

# Only core is admitted on the MCP routes. A person's tools and files are reached
# through core, never directly. Per-person access is the person gate on the
# server spec.
MCP_ROUTE_CALLERS = frozenset({"core"})

# How long a boot, and a reload that starts a new instance, waits for the first
# connection before it moves on. The supervisor keeps retrying in the background
# either way, so this only decides how long the caller holds.
BOOT_CONNECT_TIMEOUT_S = 30.0


def _new_instances(app: Starlette, name: str, spec: ServerSpec) -> list[Upstream]:
    """Create the upstream instances for one external server and register them.

    A server without identities gets one shared instance; an identity server gets
    one instance per person. The instances are registered in ``app.state.upstreams``
    but not started.
    """
    upstreams: dict[str, Upstream] = app.state.upstreams
    made: list[Upstream] = []
    for person, ispec in instance_specs(spec).items():
        up = Upstream(ispec, name=instance_name(name, person), label=name, person=person)
        upstreams[up.name] = up
        made.append(up)
    return made


def _add_route(app: Starlette, name: str, spec: ServerSpec) -> None:
    """Add the route for one external server.

    An identity server gets a multiplexing route that reads its spec and instances
    from live app state, so a reload that adds or removes a person needs no route
    change. A plain server binds its single shared instance.
    """
    if spec.has_identities:
        asgi = make_multi_asgi(
            name,
            lambda n=name: app.state.specs[n],
            lambda person, n=name: app.state.upstreams.get(instance_name(n, person)),
            MCP_ROUTE_CALLERS,
        )
    else:
        asgi = make_asgi(app.state.upstreams[name], MCP_ROUTE_CALLERS)
    app.router.routes.append(UpstreamRoute(name, asgi))


def _remove_route(app: Starlette, name: str) -> None:
    app.router.routes = [
        r for r in app.router.routes if not (isinstance(r, UpstreamRoute) and r.name == name)
    ]


def _install_catalog(app: Starlette) -> list[Upstream]:
    """Build the catalog, start every external upstream instance, and add its route."""
    specs = build_catalog(config.load())
    app.state.specs = specs
    app.state.upstreams = {}
    started: list[Upstream] = []
    for name, spec in specs.items():
        made = _new_instances(app, name, spec)
        _add_route(app, name, spec)
        for up in made:
            up.start()
        started.extend(made)
    return started


async def reconcile_catalog(app: Starlette, target: str | None = None) -> dict[str, dict]:
    """Re-read joshua.yaml and apply the diff to the running upstreams.

    The diff runs per instance: a new server or a person added to ``identities``
    starts one instance, a removed server or person stops one, a connect or filter
    change restarts one, and an unchanged instance keeps its session. A targeted
    reload restarts every instance of one server. Returns
    ``{"reloaded": {...}, "failed": {...}}``; ``reloaded`` is keyed by server and
    ``failed`` by instance. Raises ``KeyError`` for an unknown ``target``.
    """
    specs = build_catalog(config.reload())
    app.state.specs = specs
    upstreams: dict[str, Upstream] = app.state.upstreams

    current_servers = {up.label for up in upstreams.values()}
    if target is not None and target not in specs and target not in current_servers:
        raise KeyError(target)
    names = [target] if target is not None else sorted(set(specs) | current_servers)

    reloaded: dict[str, dict] = {}
    failed: dict[str, str] = {}

    await asyncio.gather(
        *(
            _reconcile_server(app, n, specs.get(n), target is not None, reloaded, failed)
            for n in names
        )
    )
    logger.info({"message": "catalog reconciled", "reloaded": sorted(reloaded), "failed": failed})
    return {"reloaded": reloaded, "failed": failed}


async def _reconcile_server(
    app: Starlette,
    name: str,
    spec: ServerSpec | None,
    targeted: bool,
    reloaded: dict[str, dict],
    failed: dict[str, str],
) -> None:
    """Reconcile one server's instances against ``spec`` (None = the server is gone)."""
    upstreams: dict[str, Upstream] = app.state.upstreams
    current = {up.person: up for up in upstreams.values() if up.label == name}

    if spec is None:
        for up in list(current.values()):
            await up.stop()
            upstreams.pop(up.name, None)
        _remove_route(app, name)
        reloaded[name] = {"removed": True}
        return

    # A change between a shared instance and per-person instances rebuilds the
    # whole server, because its route type changes.
    if current and (set(current) != {None}) != spec.has_identities:
        for up in list(current.values()):
            await up.stop()
            upstreams.pop(up.name, None)
        _remove_route(app, name)
        current = {}

    fresh = not current
    desired = instance_specs(spec)
    outcomes: dict[str | None, dict] = {}

    async def one_instance(person: str | None) -> None:
        up = current.get(person)
        want = desired.get(person)
        iname = instance_name(name, person)
        if want is None:  # person removed from an identity server
            await up.stop()
            upstreams.pop(up.name, None)
            outcomes[person] = {"removed": True}
        elif up is None:  # new server or person added
            new = Upstream(want, name=iname, label=name, person=person)
            upstreams[new.name] = new
            new.start()
            if want.disabled_reason is not None:
                outcomes[person] = {"disabled": want.disabled_reason}
            elif await new.wait_connected(BOOT_CONNECT_TIMEOUT_S):
                outcomes[person] = {"tools": new.tool_count}
            else:
                failed[iname] = new.error or "did not connect"
        elif targeted or up.restart_needed(want):
            up.update_spec(want)
            if want.disabled_reason is not None:
                await up.disable()  # drop the session; the instance stays, turned off
                outcomes[person] = {"disabled": want.disabled_reason}
                return
            try:
                await up.reload()
                outcomes[person] = {"tools": up.tool_count}
            except Exception as exc:  # noqa: BLE001 — report per-instance, keep going
                failed[iname] = up.error or str(exc)
        else:
            up.update_spec(want)
            outcomes[person] = {"tools": up.tool_count, "unchanged": True}

    await asyncio.gather(*(one_instance(p) for p in set(current) | set(desired)))

    if fresh:
        _add_route(app, name, spec)

    if spec.has_identities:
        reloaded[name] = {"instances": {p: o for p, o in outcomes.items()}}
    elif None in outcomes:  # the shared instance's outcome is the server's outcome
        reloaded[name] = outcomes[None]


@asynccontextmanager
async def lifespan(app: Starlette):
    install_healthcheck_filter()
    if not load_fleet_tokens():
        raise RuntimeError(
            "no JOSHUA_TOKEN_* fleet tokens; refusing to start an unauthenticated gateway"
        )

    ups = _install_catalog(app)
    # Give initial connections a moment so /readyz reflects reality at boot;
    # stragglers keep retrying in the background (503 until connected).
    await asyncio.gather(*(u.wait_connected(BOOT_CONNECT_TIMEOUT_S) for u in ups))
    logger.info({"message": "gateway up", "hosted": sorted(app.state.specs)})
    try:
        yield
    finally:
        await asyncio.gather(
            *(u.stop() for u in app.state.upstreams.values()), return_exceptions=True
        )
        logger.info({"message": "gateway shut down"})


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def readyz(request: Request) -> JSONResponse:
    """Readiness with counts only — no server names, tool names, or inventory. The
    full inventory is ``/admin/inventory`` (admin only).

    ``errored`` counts the upstreams whose last connect or package install
    failed. It separates a misconfigured server that never connects from one that
    is still warming up, so the fault is visible without a name. ``installing``
    counts the package entries whose install is still running, and ``disabled``
    the entries turned off because a configured credential is empty. Neither is
    an error, and neither is connected. The reason for a failed install is in
    the log and in ``/admin/inventory``, never here: this body carries no name
    and no text from a config.

    **The status code stays 200, and it is not an oversight.** ``ok`` here says
    that every upstream connected, and that is not what readiness means. The
    gateway is ready when it can serve a tool call, and the ``files`` builtin
    answers whatever an upstream is doing. A 503 would take the pod out of the
    Service, so one MCP server that fails to connect would take away every
    tool, including the ones that work. The body carries the count; the code
    carries whether this container can serve."""
    upstreams: dict[str, Upstream] = getattr(request.app.state, "upstreams", {})
    total = len(upstreams)
    counts = Counter(up.status for up in upstreams.values())
    connected = counts["connected"]
    ok = total > 0 and connected == total
    return JSONResponse(
        {
            "ok": ok,
            "connected": connected,
            "errored": counts["error"],
            "installing": counts["installing"],
            "disabled": counts["disabled"],
            "total": total,
        }
    )


def build_app() -> Starlette:
    """Build a gateway app with a fresh route table.

    The upstream routes are appended during the lifespan, so each app must own its
    own route list. Tests build a throwaway app; the process uses the module
    singleton below.
    """
    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/healthz", healthz),
            Route("/readyz", readyz),
            Route("/admin/reload", admin_reload, methods=["POST"]),
            Route("/admin/calls", admin_calls),
            Route("/admin/inventory", admin_inventory),
            Route("/admin/mcp/install", admin_mcp_install, methods=["POST"]),
        ],
    )


app = build_app()
