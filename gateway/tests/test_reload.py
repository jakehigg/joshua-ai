"""`/admin/reload` reconciles the catalog; a transport error reconnects."""

from __future__ import annotations

from conftest import FIXTURE, bearer, echo_mcp, joshua_yaml
from joshua_gateway.admin import admin_reload  # noqa: F401 — module under test
from joshua_gateway.main import lifespan
from joshua_gateway.upstream import Upstream  # noqa: F401 — module under test
from starlette.testclient import TestClient


def test_admin_reload_keeps_unchanged_session(gateway):
    with TestClient(gateway()) as client:
        response = client.post("/admin/reload", headers=bearer("laptop"))
    assert response.status_code == 200
    body = response.json()
    assert body["reloaded"]["echo"]["unchanged"] is True
    assert body["failed"] == {}


def test_admin_reload_unknown_server_is_404(gateway):
    with TestClient(gateway()) as client:
        response = client.post("/admin/reload", headers=bearer("laptop"), json={"server": "nope"})
    assert response.status_code == 404


def test_targeted_reload_restarts_one(gateway):
    with TestClient(gateway()) as client:
        response = client.post("/admin/reload", headers=bearer("laptop"), json={"server": "echo"})
    assert response.status_code == 200
    assert response.json()["reloaded"]["echo"] == {"tools": 1}


def test_connect_change_restarts(gateway, config_path):
    app = gateway(echo_mcp())
    with TestClient(app) as client:
        before = app.state.upstreams["echo"]
        # Add an upstream arg (connect config) — the entry must restart. The echo
        # fixture ignores argv, so it reconnects cleanly.
        changed = echo_mcp().replace(f"    - {FIXTURE}", f"    - {FIXTURE}\n    - --extra")
        config_path.write_text(joshua_yaml(changed))
        response = client.post("/admin/reload", headers=bearer("laptop"))
    body = response.json()
    # The upstream object is reused; its spec now carries the new connect config.
    assert app.state.upstreams["echo"] is before
    assert "--extra" in app.state.upstreams["echo"].spec.connect_cfg["args"]
    assert "echo" in body["reloaded"]


async def test_reconnect_after_transport_error(gateway):
    app = gateway()
    async with lifespan(app):
        up: Upstream = app.state.upstreams["echo"]
        assert up.connected
        up.notify_broken(RuntimeError("boom"))
        assert await up.wait_connected(30.0)
        assert up.connected
