"""Point the agent's MCP servers at the gateway.

The gateway holds every upstream credential and enforces the per-person policy.
Core sends its own fleet bearer and the person header; it opens only the servers
that person is allowed to reach, so the SDK does not start connections the
gateway would 403. The gateway stays the enforcement point.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from joshua_shared.config import JoshuaConfig, McpServer


def _permits(server: McpServer, person_id: str | None) -> bool:
    """Mirror the gateway's per-person gate: ``allow: all`` admits everyone; a
    scoped server admits only the named person ids, never an unknown caller."""
    if server.allow == "all":
        return True
    if person_id is None or person_id == "unknown":
        return False
    return person_id in server.allow


def allowed_gateway_servers(cfg: JoshuaConfig, person_id: str | None) -> list[str]:
    """The ``mcp`` section server names this person may reach, in config order."""
    return [name for name, server in cfg.mcp.items() if _permits(server, person_id)]


def gateway_servers(
    names: Iterable[str],
    gateway_url: str,
    token: str,
    person_id: str | None,
    conversation_id: str,
    role: str = "guest",
) -> dict[str, dict[str, Any]]:
    """Build the SDK ``mcp_servers`` map that routes each name through the gateway.

    Every entry carries the fleet bearer, the person header, the conversation
    header, and the role header. The gateway records the conversation header; a
    later trust policy uses it for URL grants and taint.

    **The role is the access control, and the person is not.** Since #25 the
    corpus of Joshua is shared, so the role alone decides whether the agent
    writes. The person rides along for the audit trail. A direct message carries
    the role of that person; a group carries the role derived from its members.

    The role of a conversation cannot change inside a session. The SDK writes
    this whole map onto the command line of the CLI subprocess when the session
    starts, so a header set here holds until the session is rebuilt.
    """
    base = gateway_url.rstrip("/")
    person = person_id or "unknown"
    return {
        name: {
            "type": "http",
            "url": f"{base}/{name}",
            "headers": {
                "Authorization": f"Bearer {token}",
                "X-Joshua-Person": person,
                "X-Joshua-Conversation": conversation_id,
                "X-Joshua-Role": role,
            },
        }
        for name in names
    }
