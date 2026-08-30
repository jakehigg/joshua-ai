"""Auth on the upstream routes and the admin routes."""

from __future__ import annotations

import sys
import textwrap

import pytest
from conftest import FIXTURE, bearer
from joshua_gateway.main import build_app
from joshua_gateway.routes import make_asgi  # noqa: F401 — module under test
from starlette.testclient import TestClient


def scoped_mcp() -> str:
    """An echo entry scoped to one person (``allow: [alex]``), so not open."""
    return textwrap.dedent(f"""\
        echo:
          type: stdio
          command: {sys.executable}
          args:
            - {FIXTURE}
          allow:
            - alex
          tools:
            allow:
              - echo
        """)


def test_upstream_route_requires_bearer(gateway):
    with TestClient(gateway()) as client:
        assert client.get("/echo").status_code == 401


def test_upstream_route_invalid_bearer(gateway):
    with TestClient(gateway()) as client:
        assert client.get("/echo", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_upstream_route_forbids_non_mcp_caller(gateway):
    # MCP routes admit core only; ci authenticates but is not admitted.
    with TestClient(gateway()) as client:
        assert client.get("/echo", headers=bearer("ci")).status_code == 403


def test_scoped_server_is_not_routable(gateway):
    # allow: [alex] is not open, so no request is routable yet (person policy).
    with TestClient(gateway(scoped_mcp())) as client:
        assert client.get("/echo", headers=bearer("core")).status_code == 403


def test_admin_requires_bearer(gateway):
    with TestClient(gateway()) as client:
        assert client.get("/admin/inventory").status_code == 401


def test_admin_forbids_core(gateway):
    # core is a valid identity but not an admin caller.
    with TestClient(gateway()) as client:
        assert client.get("/admin/inventory", headers=bearer("core")).status_code == 403
        assert client.get("/admin/calls", headers=bearer("core")).status_code == 403
        assert client.post("/admin/reload", headers=bearer("core")).status_code == 403


def test_admin_allows_admin_caller(gateway):
    with TestClient(gateway()) as client:
        assert client.get("/admin/inventory", headers=bearer("laptop")).status_code == 200
        assert client.get("/admin/calls", headers=bearer("ci")).status_code == 200


def test_startup_fails_without_fleet_tokens(monkeypatch):
    for name in [k for k in __import__("os").environ if k.startswith("JOSHUA_TOKEN_")]:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="no JOSHUA_TOKEN"):
        with TestClient(build_app()):
            pass
