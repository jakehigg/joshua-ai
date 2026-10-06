"""`GET /v1/version` on channels: the gateway and the operator read it, nobody else."""

from __future__ import annotations

import pytest
from joshua_channels.app import build_app
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("JOSHUA_BUILD_VERSION", "")
    monkeypatch.setenv("JOSHUA_BUILD_REVISION", "abcdef0123456789")
    monkeypatch.setenv("JOSHUA_BUILD_REF", "branch-main")
    monkeypatch.setenv("JOSHUA_TOKEN_GATEWAY", "gw")
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", "lap")
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", "core")
    # No `with`: the lifespan loads a config, and the route does not need one.
    return TestClient(build_app())


@pytest.mark.parametrize("token", ["gw", "lap"])
def test_gateway_and_laptop_read_a_branch_build(client: TestClient, token: str) -> None:
    response = client.get("/v1/version", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["container"] == "channels"
    assert body["version"] == ""
    assert body["display"] == "branch-main@abcdef0"


def test_core_is_forbidden_and_no_bearer_is_unauthorized(client: TestClient) -> None:
    assert client.get("/v1/version", headers={"Authorization": "Bearer core"}).status_code == 403
    assert client.get("/v1/version").status_code == 401
