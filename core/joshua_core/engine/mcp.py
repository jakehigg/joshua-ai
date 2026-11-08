"""Point the agent's MCP servers at the gateway.

``joshua_shared.gateway_mcp`` holds the code, so the assignments container
builds the map of a person in the same way. This module exports it for the
engine.
"""

from __future__ import annotations

from joshua_shared.gateway_mcp import allowed_gateway_servers, gateway_servers

__all__ = ["allowed_gateway_servers", "gateway_servers"]
