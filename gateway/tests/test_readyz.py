"""`/healthz` and `/readyz` shape."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from conftest import bearer
from joshua_gateway.main import build_app, readyz  # noqa: F401 — module under test
from starlette.testclient import TestClient


def _readyz_body(*statuses: str) -> dict:
    """Call ``readyz`` with fake upstreams of the given statuses and return its body.

    A never-connecting upstream would make a full app boot wait out the readiness
    timeout, so the counts are checked against the ``status`` values directly.
    """
    upstreams = {f"s{i}": SimpleNamespace(status=s) for i, s in enumerate(statuses)}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(upstreams=upstreams)))
    return json.loads(asyncio.run(readyz(request)).body)


def test_healthz_is_ok_only(gateway):
    with TestClient(gateway()) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_readyz_reports_counts_only(gateway):
    with TestClient(gateway()) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "connected": 1,
        "errored": 0,
        "installing": 0,
        "disabled": 0,
        "total": 1,
    }


def test_readyz_counts_an_errored_upstream_and_is_not_ok():
    # A misconfigured server (last connect failed) shows in ``errored`` and drops
    # ``ok``, while a warming-up server does not count as errored.
    assert _readyz_body("connected", "error", "connecting") == {
        "ok": False,
        "connected": 1,
        "errored": 1,
        "installing": 0,
        "disabled": 0,
        "total": 3,
    }


def test_readyz_counts_installing_and_disabled_apart_from_errored():
    """An install that is still running and an entry with no credential are
    neither connected nor errored, so an operator can tell them apart."""
    assert _readyz_body("connected", "installing", "disabled") == {
        "ok": False,
        "connected": 1,
        "errored": 0,
        "installing": 1,
        "disabled": 1,
        "total": 3,
    }


def test_readyz_leaks_no_names(gateway):
    with TestClient(gateway()) as client:
        response = client.get("/readyz")
    # No server name, tool name, or inventory key may appear.
    assert "echo" not in response.text
    assert "secret_tool" not in response.text
    assert "tools" not in response.text


def test_readyz_needs_no_auth(gateway):
    with TestClient(gateway()) as client:
        assert client.get("/readyz").status_code == 200
        # The full inventory (with tool names) stays behind admin auth.
        assert client.get("/admin/inventory", headers=bearer("laptop")).status_code == 200


# -- an errored upstream is not an unready container ---------------------------


def _readyz_response(*statuses: str):
    upstreams = {f"s{i}": SimpleNamespace(status=s) for i, s in enumerate(statuses)}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(upstreams=upstreams)))
    return asyncio.run(readyz(request))


def test_an_errored_upstream_keeps_the_200():
    """The files builtin answers whatever an upstream is doing.

    A 503 takes the pod out of the Service, so one MCP server that fails to
    connect would take away every tool, including the ones that work.
    """
    response = _readyz_response("connected", "error")
    assert response.status_code == 200
    assert json.loads(response.body)["ok"] is False


def test_every_upstream_connected_also_gives_the_200():
    assert _readyz_response("connected", "connected").status_code == 200
