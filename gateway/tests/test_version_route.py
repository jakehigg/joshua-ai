"""`GET /v1/version` on the gateway: the operator reads it; core does not."""

from __future__ import annotations

import pytest
from conftest import TOKENS, bearer
from joshua_gateway.main import build_app
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("JOSHUA_BUILD_VERSION", raising=False)
    monkeypatch.delenv("JOSHUA_BUILD_REVISION", raising=False)
    monkeypatch.delenv("JOSHUA_BUILD_REF", raising=False)
    for identity, token in TOKENS.items():
        monkeypatch.setenv(f"JOSHUA_TOKEN_{identity.upper()}", token)
    # No `with`: the lifespan starts every upstream, and the route needs none.
    return TestClient(build_app())


@pytest.mark.parametrize("identity", ["gateway", "laptop"])
def test_gateway_and_laptop_read_a_dev_build(client: TestClient, identity: str) -> None:
    response = client.get("/v1/version", headers=bearer(identity))
    assert response.status_code == 200
    assert response.json()["container"] == "gateway"
    assert response.json()["display"] == "dev"


def test_core_is_forbidden_and_no_bearer_is_unauthorized(client: TestClient) -> None:
    assert client.get("/v1/version", headers=bearer("core")).status_code == 403
    assert client.get("/v1/version").status_code == 401
