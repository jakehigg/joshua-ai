"""Catalog build, tool-filter compile, and the reload diff."""

from __future__ import annotations

import textwrap

from conftest import bearer, echo_mcp, joshua_yaml
from joshua_gateway.catalog import ALL, ToolFilter, build_catalog
from joshua_shared import config
from starlette.testclient import TestClient

ROSTER = """
name: Test
timezone: America/New_York
people:
  - id: alex
    name: Alex
  - id: mia
    name: Mia
"""


def build_from(mcp_block: str, env: dict[str, str] | None = None):
    text = ROSTER + "\nmcp:\n" + textwrap.indent(textwrap.dedent(mcp_block), "  ")
    cfg = config.parse(text, env or {}, source="test.yaml")
    return build_catalog(cfg)


# -- tool filter -----------------------------------------------------------


def test_filter_absent_allow_permits_all():
    f = ToolFilter()
    assert f.permits("anything") is True


def test_filter_allow_globs():
    f = ToolFilter(allow=("get_*", "list_*"))
    assert f.permits("get_weather") is True
    assert f.permits("list_items") is True
    assert f.permits("delete_all") is False


def test_filter_deny_beats_allow():
    f = ToolFilter(allow=("*",), deny=("*_admin",))
    assert f.permits("get_weather") is True
    assert f.permits("do_admin") is False


def test_filter_unmatched_allow_reports_typos():
    f = ToolFilter(allow=("get_*", "typo_*"))
    assert f.unmatched_allow(["get_weather", "get_time"]) == ["typo_*"]


# -- build_catalog ---------------------------------------------------------


def test_build_stdio_connect_cfg_and_filter():
    catalog = build_from(
        """
        weather:
          type: stdio
          command: uvx
          args:
            - mcp-weather
          allow: all
          tools:
            allow:
              - "get_*"
            deny:
              - "*_admin"
            class:
              "get_*": read
          url_args: none
        """
    )
    spec = catalog["weather"]
    assert spec.is_builtin is False
    assert spec.transport == "stdio"
    assert spec.connect_cfg == {"type": "stdio", "command": "uvx", "args": ["mcp-weather"]}
    assert spec.tool_filter.permits("get_x") is True
    assert spec.tool_filter.permits("get_admin") is False
    assert spec.tool_classes == {"get_*": "read"}
    assert spec.url_args == "none"
    assert spec.allow_persons is ALL
    assert spec.is_open is True


def test_build_builtin_has_no_transport():
    catalog = build_from(
        """
        files:
          kind: builtin
          allow: all
          options:
            verbose: false
        """
    )
    spec = catalog["files"]
    assert spec.is_builtin is True
    assert spec.builtin == "files"
    assert spec.transport is None
    assert spec.options == {"verbose": "false"}


def test_build_http_scoped_and_identities():
    catalog = build_from(
        """
        spotify:
          type: http
          url: https://spotify-mcp.example/mcp
          allow:
            - alex
          identities:
            alex:
              headers:
                Authorization: "Bearer ${SPOTIFY_TOKEN_ALEX}"
        """,
        env={"SPOTIFY_TOKEN_ALEX": "sj"},
    )
    spec = catalog["spotify"]
    assert spec.transport == "http"
    assert spec.connect_cfg == {"type": "http", "url": "https://spotify-mcp.example/mcp"}
    assert spec.is_open is False
    assert spec.allow_persons == frozenset({"alex"})
    assert spec.identities == {"alex": {"headers": {"Authorization": "Bearer sj"}}}


def test_view_pattern_two_entries_one_upstream():
    catalog = build_from(
        """
        tracker-readonly:
          type: stdio
          command: mcp-tracker
          allow: all
          tools:
            allow:
              - "get_*"
        tracker-full:
          type: stdio
          command: mcp-tracker
          allow: all
        """
    )
    assert set(catalog) == {"tracker-readonly", "tracker-full"}
    assert catalog["tracker-readonly"].tool_filter.permits("delete_x") is False
    assert catalog["tracker-full"].tool_filter.permits("delete_x") is True


# -- reload diff (app level) ----------------------------------------------


def test_reload_applies_new_filter_without_restarting_container(gateway, config_path):
    """Changing tools.allow in the file and reloading changes tools/list."""
    app = gateway(echo_mcp(tools_allow="echo"))
    with TestClient(app) as client:
        # secret_tool is not in the allowlist yet.
        inv = client.get("/admin/inventory", headers=bearer("laptop")).json()
        assert inv["servers"]["echo"]["tools"] == ["echo"]

        config_path.write_text(joshua_yaml(echo_mcp(tools_allow='"*"')))
        resp = client.post("/admin/reload", headers=bearer("laptop"))
        assert resp.status_code == 200
        assert "echo" in resp.json()["reloaded"]

        inv = client.get("/admin/inventory", headers=bearer("laptop")).json()
        assert sorted(inv["servers"]["echo"]["tools"]) == ["echo", "secret_tool", "whoami"]


def test_reload_starts_added_and_stops_removed(gateway, config_path):
    app = gateway(echo_mcp())
    with TestClient(app) as client:
        # Add a second entry pointing at the same fixture under a new name.
        two = echo_mcp() + echo_mcp().replace("echo:", "echo2:", 1)
        config_path.write_text(joshua_yaml(two))
        resp = client.post("/admin/reload", headers=bearer("laptop"))
        assert resp.status_code == 200
        assert "echo2" in app.state.upstreams

        # Remove echo2 again.
        config_path.write_text(joshua_yaml(echo_mcp()))
        resp = client.post("/admin/reload", headers=bearer("laptop"))
        assert resp.status_code == 200
        assert "echo2" not in app.state.upstreams
        assert resp.json()["reloaded"]["echo2"] == {"removed": True}
