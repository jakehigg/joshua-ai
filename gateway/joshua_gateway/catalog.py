"""Build the gateway's upstream catalog from the ``mcp:`` section of joshua.yaml.

The shared loader has already expanded ``${VAR}`` and validated the models, so this
module only turns each ``McpServer`` into a ``ServerSpec`` the supervisor and routes
consume. One entry is one route; two entries can name the same upstream under
different filters (the "view" pattern).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from typing import Any

from joshua_shared.config import JoshuaConfig, McpServer

# Sentinel: the server is open to every person (``allow: all``).
ALL = frozenset({"*"})


@dataclass(frozen=True)
class ToolFilter:
    """fnmatch allow/deny filter. ``deny`` beats ``allow``; empty ``allow`` = all."""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    def permits(self, name: str) -> bool:
        if any(fnmatch(name, pattern) for pattern in self.deny):
            return False
        if not self.allow:
            return True
        return any(fnmatch(name, pattern) for pattern in self.allow)

    def unmatched_allow(self, names: list[str]) -> list[str]:
        """Return ``allow`` patterns that match no name in ``names`` (typo hints)."""
        return [p for p in self.allow if not any(fnmatch(n, p) for n in names)]


@dataclass(frozen=True)
class ServerSpec:
    """One catalog entry, resolved for the supervisor and the routes."""

    name: str
    builtin: str | None  # builtin server name (files) or None for an external upstream
    transport: str | None  # stdio | http | sse for an external upstream, else None
    connect_cfg: dict[str, Any]  # command/args/env or url/headers, for upstream_streams
    tool_filter: ToolFilter
    tool_classes: dict[str, str]  # reserved for the trust policy: pattern -> read|write-local|act
    url_args: str  # reserved for the trust policy: none | grant-required
    allow_persons: frozenset[str]  # person ids, or the ALL sentinel
    identities: dict[str, dict[str, Any]]  # person -> connect overrides (identities ticket)
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def is_builtin(self) -> bool:
        return self.builtin is not None

    @property
    def has_identities(self) -> bool:
        """True when the entry runs one upstream instance per person."""
        return bool(self.identities)

    @property
    def is_open(self) -> bool:
        """True when the entry is open to every person (``allow: all``)."""
        return self.allow_persons is ALL

    def permits_person(self, person: str | None) -> bool:
        """True when ``person`` may reach this server.

        ``allow: all`` admits everyone, including no person and ``unknown``. A
        scoped server admits only the named person ids; no person and ``unknown``
        are denied.
        """
        if self.allow_persons is ALL:
            return True
        if person is None or person == "unknown":
            return False
        return person in self.allow_persons


def permits(server: ServerSpec, tool: str, person: str | None) -> bool:
    """True when ``person`` may use ``tool`` on ``server``.

    Combines the per-server person gate with the tool filter. The signature holds
    room for a per-person tool override later.
    """
    return server.permits_person(person) and server.tool_filter.permits(tool)


def _tool_filter(server: McpServer) -> ToolFilter:
    if server.tools is None:
        return ToolFilter()
    return ToolFilter(allow=tuple(server.tools.allow), deny=tuple(server.tools.deny))


def _connect_cfg(server: McpServer) -> dict[str, Any]:
    if server.type == "stdio":
        cfg: dict[str, Any] = {
            "type": "stdio",
            "command": server.command,
            "args": list(server.args),
        }
        if server.env:
            cfg["env"] = dict(server.env)
        return cfg
    cfg = {"type": server.type, "url": server.url}
    if server.headers:
        cfg["headers"] = dict(server.headers)
    return cfg


def _identity_overrides(server: McpServer) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for person, identity in server.identities.items():
        overrides: dict[str, Any] = {}
        if identity.headers:
            overrides["headers"] = dict(identity.headers)
        if identity.url is not None:
            overrides["url"] = identity.url
        if identity.command is not None:
            overrides["command"] = identity.command
        if identity.args:
            overrides["args"] = list(identity.args)
        if identity.env:
            overrides["env"] = dict(identity.env)
        out[person] = overrides
    return out


def _build_spec(name: str, server: McpServer) -> ServerSpec:
    allow_persons = ALL if server.allow == "all" else frozenset(server.allow)
    tool_classes = dict(server.tools.tool_class) if server.tools else {}
    common = {
        "name": name,
        "tool_filter": _tool_filter(server),
        "tool_classes": tool_classes,
        "url_args": server.url_args,
        "allow_persons": allow_persons,
        "identities": _identity_overrides(server),
    }
    if server.kind is not None:
        options = dict(server.options)
        return ServerSpec(
            builtin=name,
            transport=None,
            connect_cfg={"type": "builtin", "builtin": name, "options": options},
            options=options,
            **common,
        )
    return ServerSpec(
        builtin=None,
        transport=server.type,
        connect_cfg=_connect_cfg(server),
        **common,
    )


def build_catalog(cfg: JoshuaConfig) -> dict[str, ServerSpec]:
    """Return ``{name: ServerSpec}`` for every entry in ``cfg.mcp``."""
    return {name: _build_spec(name, server) for name, server in cfg.mcp.items()}


def instance_name(name: str, person: str | None) -> str:
    """The upstream instance name for a server route and a person.

    A server without identities has one shared instance named for the route. An
    identity server has one instance per person, named ``route:person``.
    """
    return name if person is None else f"{name}:{person}"


def _merge_connect(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge an identity's connect overrides onto the base connect config.

    A dict value (``env``, ``headers``) merges key by key; any other value
    (``url``, ``command``, ``args``) replaces the base value.
    """
    merged = {key: dict(value) if isinstance(value, dict) else value for key, value in base.items()}
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def instance_specs(spec: ServerSpec) -> dict[str | None, ServerSpec]:
    """Return the upstream instances for one external server.

    A server without identities has a single shared instance keyed by ``None``. An
    identity server has one instance per person, each carrying that person's
    connect overrides deep-merged onto the base connect config; there is no shared
    instance.
    """
    if not spec.identities:
        return {None: spec}
    return {
        person: replace(spec, connect_cfg=_merge_connect(spec.connect_cfg, overrides))
        for person, overrides in spec.identities.items()
    }
