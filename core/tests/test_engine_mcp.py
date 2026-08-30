"""Unit tests for the gateway MCP wiring: per-person allow filtering and the
headers every server entry carries."""

from __future__ import annotations

import pytest
from joshua_core.engine.mcp import allowed_gateway_servers, gateway_servers
from joshua_shared import config as config_module

CONFIG_YAML = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    role: member
  - id: mia
    name: Mia
    role: member
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


@pytest.fixture
def cfg():
    return config_module.parse(CONFIG_YAML, env={}, source="<test>")


def test_open_server_allows_everyone(cfg):
    assert allowed_gateway_servers(cfg, "alex") == ["files", "private"]
    assert allowed_gateway_servers(cfg, "mia") == ["files"]


def test_unknown_and_none_get_only_open_servers(cfg):
    assert allowed_gateway_servers(cfg, None) == ["files"]
    assert allowed_gateway_servers(cfg, "unknown") == ["files"]


def test_scoped_server_omitted_for_disallowed_person(cfg):
    names = allowed_gateway_servers(cfg, "mia")
    servers = gateway_servers(names, "http://gw:8000", "tok", "mia", "conv-9")
    assert "private" not in servers
    assert "files" in servers


def test_gateway_servers_headers(cfg):
    names = allowed_gateway_servers(cfg, "alex")
    servers = gateway_servers(names, "http://gw:8000/", "secret-tok", "alex", "conv-9")
    for name in ("files", "private"):
        entry = servers[name]
        assert entry["type"] == "http"
        assert entry["url"] == f"http://gw:8000/{name}"
        headers = entry["headers"]
        assert headers["Authorization"] == "Bearer secret-tok"
        assert headers["X-Joshua-Person"] == "alex"
        assert headers["X-Joshua-Conversation"] == "conv-9"


def test_gateway_servers_person_defaults_to_unknown(cfg):
    servers = gateway_servers(["files"], "http://gw:8000", "tok", None, "conv-1")
    assert servers["files"]["headers"]["X-Joshua-Person"] == "unknown"
