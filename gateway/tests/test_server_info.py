"""The gateway keeps what an upstream says it is at ``initialize``.

``serverInfo`` carries a name and a version. An addon reports its release
there, so the inventory can say which release of an addon the gateway talks
to. A builtin has no such fields.
"""

from __future__ import annotations

import sys

from conftest import FIXTURE, bearer
from starlette.testclient import TestClient


def echo_with_version(version: str | None) -> str:
    """The files builtin and the echo fixture, with ``ECHO_VERSION`` when given."""
    block = (
        "files:\n"
        "  kind: builtin\n"
        "echo:\n"
        "  type: stdio\n"
        f"  command: {sys.executable}\n"
        "  args:\n"
        f"    - {FIXTURE}\n"
    )
    if version:
        block += f'  env:\n    ECHO_VERSION: "{version}"\n'
    return block


def _inventory(client: TestClient) -> dict:
    return client.get("/admin/inventory", headers=bearer("laptop")).json()["servers"]


def test_the_inventory_shows_the_upstream_name_and_version(gateway):
    with TestClient(gateway(echo_with_version("2026.10.1"))) as client:
        servers = _inventory(client)
    echo = servers["echo"]
    assert echo["server_name"] == "echo"
    assert echo["server_version"] == "2026.10.1"
    assert echo["instances"][0]["server_name"] == "echo"
    assert echo["instances"][0]["server_version"] == "2026.10.1"


def test_an_upstream_with_no_version_shows_none_and_nothing_fails(gateway):
    with TestClient(gateway(echo_with_version(None))) as client:
        servers = _inventory(client)
    assert servers["echo"]["server_name"] == "echo"
    assert servers["echo"]["server_version"] is None


def test_a_builtin_has_no_server_fields(gateway):
    with TestClient(gateway(echo_with_version(None))) as client:
        servers = _inventory(client)
    assert "server_name" not in servers["files"]
    assert "server_version" not in servers["files"]
