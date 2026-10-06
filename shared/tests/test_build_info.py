"""`joshua_shared.build_info`: what an image knows about itself, and the route."""

from __future__ import annotations

import logging

import pytest
from joshua_shared import build_info
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

FULL = {
    "JOSHUA_BUILD_VERSION": "2026.10.1",
    "JOSHUA_BUILD_REVISION": "10ef1a4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c",
    "JOSHUA_BUILD_REF": "v2026.10.1",
    "JOSHUA_BUILD_DATE": "2026-10-06T12:00:00Z",
}
BRANCH = {
    "JOSHUA_BUILD_VERSION": "",
    "JOSHUA_BUILD_REVISION": "10ef1a4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c",
    "JOSHUA_BUILD_REF": "branch-main",
    "JOSHUA_BUILD_DATE": "2026-10-06T12:00:00Z",
}


def test_a_release_build_reads_every_field() -> None:
    info = build_info.read(FULL)
    assert info.version == "2026.10.1"
    assert info.revision.startswith("10ef1a4")
    assert info.ref == "v2026.10.1"
    assert info.built_at == "2026-10-06T12:00:00Z"
    assert info.display() == "2026.10.1"


def test_a_branch_build_has_no_version_and_shows_its_ref_and_revision() -> None:
    info = build_info.read(BRANCH)
    assert info.version == ""
    assert info.display() == "branch-main@10ef1a4"


def test_a_revision_with_no_ref_still_displays() -> None:
    info = build_info.read({"JOSHUA_BUILD_REVISION": "abcdef0123456789"})
    assert info.display() == "branch@abcdef0"


def test_an_empty_environment_is_dev() -> None:
    info = build_info.read({})
    assert info.version == "dev"
    assert info.display() == "dev"
    assert info.as_dict() == {
        "version": "dev",
        "revision": "",
        "ref": "",
        "built_at": "",
        "display": "dev",
    }


def test_as_dict_holds_the_five_keys_and_nothing_else() -> None:
    assert set(build_info.read(FULL).as_dict()) == {
        "version",
        "revision",
        "ref",
        "built_at",
        "display",
    }


def test_log_build_writes_one_line(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    for key, value in FULL.items():
        monkeypatch.setenv(key, value)
    logger = logging.getLogger("test.build")
    with caplog.at_level(logging.INFO, logger="test.build"):
        build_info.log_build(logger, "core")
    assert len(caplog.records) == 1
    record = caplog.records[0].msg
    assert record["message"] == "build"
    assert record["container"] == "core"
    assert record["display"] == "2026.10.1"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for key, value in FULL.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("JOSHUA_TOKEN_GATEWAY", "gw")
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", "lap")
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", "ch")
    app = Starlette(routes=[Route("/v1/version", build_info.version_endpoint("core"))])
    return TestClient(app)


@pytest.mark.parametrize("token", ["gw", "lap"])
def test_gateway_and_laptop_read_the_version(client: TestClient, token: str) -> None:
    response = client.get("/v1/version", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["container"] == "core"
    assert body["version"] == "2026.10.1"
    assert body["display"] == "2026.10.1"
    assert set(body) == {"container", "version", "revision", "ref", "built_at", "display"}


def test_another_identity_is_forbidden(client: TestClient) -> None:
    response = client.get("/v1/version", headers={"Authorization": "Bearer ch"})
    assert response.status_code == 403


def test_no_bearer_is_unauthorized(client: TestClient) -> None:
    assert client.get("/v1/version").status_code == 401
    assert client.get("/v1/version", headers={"Authorization": "Bearer nope"}).status_code == 401
