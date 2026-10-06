"""`GET /v1/version` on core: the gateway and the operator read it, nobody else."""

from __future__ import annotations

import pytest
from joshua_core.main import build_app
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("JOSHUA_BUILD_VERSION", "2026.10.1")
    monkeypatch.setenv("JOSHUA_BUILD_REVISION", "abcdef0123456789")
    monkeypatch.setenv("JOSHUA_TOKEN_GATEWAY", "gw")
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", "lap")
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", "ch")
    # No `with`: the lifespan needs a database, and the route does not.
    return TestClient(build_app())


@pytest.mark.parametrize("token", ["gw", "lap"])
def test_gateway_and_laptop_read_the_version(client: TestClient, token: str) -> None:
    response = client.get("/v1/version", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["container"] == "core"
    assert response.json()["display"] == "2026.10.1"


def test_channels_is_forbidden_and_no_bearer_is_unauthorized(client: TestClient) -> None:
    assert client.get("/v1/version", headers={"Authorization": "Bearer ch"}).status_code == 403
    assert client.get("/v1/version").status_code == 401
