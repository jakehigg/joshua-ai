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
    assert response.json() == {"ok": True, "connected": 1, "errored": 0, "total": 1}


def test_readyz_counts_an_errored_upstream_and_is_not_ok():
    # A misconfigured server (last connect failed) shows in ``errored`` and drops
    # ``ok``, while a warming-up server does not count as errored.
    assert _readyz_body("connected", "error", "connecting") == {
        "ok": False,
        "connected": 1,
        "errored": 1,
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


# -- the status code is the answer ---------------------------------------------


def _readyz_response(*statuses: str):
    upstreams = {f"s{i}": SimpleNamespace(status=s) for i, s in enumerate(statuses)}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(upstreams=upstreams)))
    return asyncio.run(readyz(request))


def test_readyz_answers_503_when_an_upstream_errored():
    """A probe reads the code. A body under a 200 is a probe that cannot fail."""
    assert _readyz_response("connected", "error").status_code == 503


def test_readyz_answers_200_when_every_upstream_is_connected():
    assert _readyz_response("connected", "connected").status_code == 200
