"""What this gateway runs, for ``instance_addons``.

The gateway owns the catalog and every upstream. ``main`` hands this module a
provider at start, and the builtin reads a snapshot from it on each call. The
tests hand it a fake. There is no HTTP between the builtin and the gateway it
runs in.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from joshua_gateway.catalog import ServerSpec


@dataclass(frozen=True)
class Entry:
    name: str
    kind: str  # builtin | package | http | stdio | sse
    status: str
    tool_count: int | None
    version: str | None  # the resolved package version, or the upstream's serverInfo version
    server_name: str | None
    pinned: str | None  # the package: spec, for a package entry

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "tool_count": self.tool_count,
            "version": self.version,
        }
        if self.server_name is not None:
            out["server_name"] = self.server_name
        if self.pinned is not None:
            out["package"] = self.pinned
        return out


Provider = Callable[[], list[Entry]]

_provider: Provider | None = None


def set_provider(provider: Provider | None) -> None:
    """Hand the builtin the way to read the live inventory."""
    global _provider
    _provider = provider


def entries() -> list[Entry] | None:
    """The live inventory, or None when no gateway has bound one."""
    return _provider() if _provider is not None else None


def snapshot(specs: dict[str, ServerSpec], upstreams: dict[str, Any]) -> list[Entry]:
    """Build the inventory from the gateway's catalog and its upstreams."""
    out: list[Entry] = []
    for name, spec in specs.items():
        instances = [up for up in upstreams.values() if up.label == name]
        first = instances[0] if instances else None
        if spec.is_builtin:
            kind = "builtin"
        elif spec.package is not None:
            kind = "package"
        else:
            kind = spec.transport or "stdio"
        status = _status(instances)
        version = None
        server_name = None
        if first is not None and not spec.is_builtin:
            known = next((up for up in instances if up.server_name), first)
            server_name = known.server_name
            version = known.server_version
            if kind == "package":
                record = next((up.install for up in instances if up.install), None) or {}
                version = record.get("resolved") or version
        out.append(
            Entry(
                name=name,
                kind=kind,
                status=status,
                tool_count=first.tool_count if first is not None else None,
                version=version,
                server_name=server_name,
                pinned=spec.package,
            )
        )
    return out


def _status(instances: list[Any]) -> str:
    statuses = {up.status for up in instances}
    if not statuses:
        return "absent"
    if statuses == {"connected"}:
        return "connected"
    for state in ("error", "installing", "disabled"):
        if state in statuses:
            return state
    return "connecting"
