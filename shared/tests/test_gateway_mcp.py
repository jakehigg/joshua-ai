"""The gateway MCP map: the servers a person may reach, and the headers of each."""

from __future__ import annotations

from joshua_shared import config
from joshua_shared.gateway_mcp import allowed_gateway_servers, gateway_servers

CONFIG = """
name: Home
timezone: UTC
people:
  - id: alex
    name: Alex
  - id: mia
    name: Mia
mcp:
  files:
    kind: builtin
    allow: all
  private:
    type: http
    url: http://upstream/private
    allow:
      - alex
"""


def test_a_scoped_server_admits_only_the_named_person() -> None:
    cfg = config.parse(CONFIG, {}, source="test.yaml")
    assert allowed_gateway_servers(cfg, "alex") == ["files", "private"]
    assert allowed_gateway_servers(cfg, "mia") == ["files"]
    assert allowed_gateway_servers(cfg, None) == ["files"]


def test_each_entry_carries_the_bearer_and_the_person_headers() -> None:
    servers = gateway_servers(
        ["files"], "http://gateway:8000/", "tok", "alex", "assignment:7", role="member"
    )
    entry = servers["files"]
    assert entry["url"] == "http://gateway:8000/files"
    assert entry["headers"] == {
        "Authorization": "Bearer tok",
        "X-Joshua-Person": "alex",
        "X-Joshua-Conversation": "assignment:7",
        "X-Joshua-Role": "member",
    }
