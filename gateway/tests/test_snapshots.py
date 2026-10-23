"""The snapshot route, ``POST /v1/snapshots/{name}``.

The config is the allowlist. Each refusal rule has a negative test here, and a
call proves the person routing, the timeout, the text joining, and a call log
entry that holds no content. The upstream is the ``snapshot_server.py``
fixture over stdio.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx2
import mcp_types as types
import pytest
from conftest import TOKENS, bearer
from joshua_gateway import snapshots
from joshua_gateway.catalog import build_catalog
from joshua_gateway.main import build_app, lifespan
from joshua_gateway.observability import CALL_LOG
from joshua_shared import config
from joshua_shared.config import Snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "snapshot_server.py"

SNAPSHOTS = {
    "open-items": ("tasks", "list_items", "shared"),
    "failing": ("tasks", "fail_items", "shared"),
    "slow": ("tasks", "slow_items", "shared"),
    "wipe": ("tasks", "wipe_items", "shared"),
    "safe-wipe": ("tasks", "safe_wipe", "shared"),
    "hidden": ("tasks", "add_item", "shared"),
    "undeclared": ("tasks", "no_such_tool", "shared"),
    "act-items": ("acts", "list_items", "shared"),
    "write-items": ("acts", "add_item", "shared"),
    "alex-items": ("mine", "list_items", "alex"),
    "mia-items": ("mine", "list_items", "mia"),
    "own-tasks": ("scheduling", "list_tasks", "alex"),
}


def _yaml() -> str:
    snaps = "".join(
        f"    - name: {name}\n      server: {server}\n      tool: {tool}\n"
        f"      every: 5m\n      scope: {scope}\n"
        + ("      args:\n        list: groceries\n" if name == "open-items" else "")
        for name, (server, tool, scope) in SNAPSHOTS.items()
    )
    stdio = f"    type: stdio\n    command: {sys.executable}\n    args:\n      - {FIXTURE}\n"
    return (
        "name: Test\n"
        "timezone: America/New_York\n"
        "people:\n"
        "  - id: alex\n    name: Alex\n"
        "  - id: mia\n    name: Mia\n"
        "  - id: sam\n    name: Sam\n"
        "memory:\n  snapshots:\n"
        f"{snaps}"
        "mcp:\n"
        "  tasks:\n"
        f"{stdio}"
        "    allow: all\n"
        "    tools:\n      deny:\n        - add_item\n"
        "  acts:\n"
        f"{stdio}"
        "    allow: all\n"
        "    tools:\n      class:\n        list_items: act\n        add_*: write-local\n"
        "  mine:\n"
        f"{stdio}"
        "    allow:\n      - alex\n      - mia\n"
        "    identities:\n"
        "      alex:\n        env:\n          SNAP_IDENTITY: alex-value\n"
        "      mia:\n        env:\n          SNAP_IDENTITY: mia-value\n"
    )


@pytest.fixture
def app(monkeypatch, config_path):
    config_path.write_text(_yaml())
    monkeypatch.setattr(config, "_cache", None)
    for identity, token in TOKENS.items():
        monkeypatch.setenv(f"JOSHUA_TOKEN_{identity.upper()}", token)
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    return build_app()


async def _post(app, name: str, identity: str | None = "core") -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    headers = bearer(identity) if identity else {}
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=30
    ) as client:
        return await client.post(f"/v1/snapshots/{name}", headers=headers)


def _entries(name: str) -> list[dict]:
    return [e for e in CALL_LOG.recent(limit=0) if e.get("snapshot") == name]


# -- auth ------------------------------------------------------------------


async def test_no_bearer_is_401(app) -> None:
    async with lifespan(app):
        assert (await _post(app, "open-items", None)).status_code == 401


@pytest.mark.parametrize("identity", ["laptop", "channels", "gateway"])
async def test_a_caller_that_is_not_core_is_403(app, identity) -> None:
    async with lifespan(app):
        response = await _post(app, "open-items", identity)
    assert response.status_code == 403


# -- a good call -----------------------------------------------------------


async def test_a_call_joins_the_text_parts(app) -> None:
    async with lifespan(app):
        response = await _post(app, "open-items")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["is_error"] is False
    assert body["text"] == "items for groceries\n"
    assert body["fetched_at"]


async def test_the_call_log_holds_no_content(app) -> None:
    async with lifespan(app):
        await _post(app, "open-items")
    entries = _entries("open-items")
    assert entries and entries[0]["status"] == "success"
    assert entries[0]["tool"] == "list_items"
    assert entries[0]["identity"] == "core"
    assert "groceries" not in repr(entries)


async def test_a_tool_error_is_not_ok(app) -> None:
    async with lifespan(app):
        body = (await _post(app, "failing")).json()
    assert body["ok"] is False
    assert body["is_error"] is True
    assert body["text"] == "the list is not available"


async def test_each_person_gets_their_own_instance(app) -> None:
    async with lifespan(app):
        alex = (await _post(app, "alex-items")).json()
        mia = (await _post(app, "mia-items")).json()
    assert alex["text"].endswith("alex-value")
    assert mia["text"].endswith("mia-value")
    assert _entries("alex-items")[0]["person"] == "alex"


async def test_a_slow_tool_times_out(app, monkeypatch) -> None:
    monkeypatch.setattr(snapshots, "SNAPSHOT_TIMEOUT_S", 0.5)
    async with lifespan(app):
        response = await _post(app, "slow")
    assert response.status_code == 504
    assert _entries("slow")[0]["error"] == "timeout"


async def test_destructive_and_read_only_is_allowed(app) -> None:
    async with lifespan(app):
        response = await _post(app, "safe-wipe")
    assert response.status_code == 200
    assert response.json()["text"] == "nothing wiped"


# -- refusals --------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "status", "reason"),
    [
        ("not-configured", 404, "unknown snapshot"),
        ("own-tasks", 404, "unknown snapshot"),
        ("hidden", 403, "tool_not_exposed"),
        ("undeclared", 403, "tool_not_exposed"),
        ("act-items", 403, "tool_class_act"),
        ("write-items", 403, "tool_class_write-local"),
        ("wipe", 403, "tool_destructive"),
    ],
)
async def test_a_refused_snapshot_calls_nothing(app, name, status, reason) -> None:
    async with lifespan(app):
        response = await _post(app, name)
    assert response.status_code == status
    assert response.json()["error"] == reason
    for entry in _entries(name):
        assert entry["status"] == "denied"


def _spec(cfg_text: str, server: str):
    return build_catalog(config.parse(cfg_text, {}, source="t.yaml"))[server]


def test_a_person_the_server_does_not_admit_is_refused() -> None:
    spec = _spec(_yaml(), "mine")
    snap = Snapshot(name="sam-items", server="mine", tool="list_items", every="5m", scope="sam")
    with pytest.raises(snapshots.SnapshotRefused) as caught:
        snapshots.check_policy(spec, snap)
    assert caught.value.reason == "person_not_allowed"


def test_shared_on_a_scoped_server_is_refused() -> None:
    spec = _spec(_yaml(), "mine")
    snap = Snapshot(name="all-items", server="mine", tool="list_items", every="5m")
    with pytest.raises(snapshots.SnapshotRefused):
        snapshots.check_policy(spec, snap)


def test_an_exact_class_wins_over_a_pattern() -> None:
    spec = _spec(_yaml(), "acts")
    assert snapshots.configured_class(spec, "list_items") == "act"
    assert snapshots.configured_class(spec, "add_item") == "write-local"
    assert snapshots.configured_class(spec, "other") is None


def test_annotations_decide_destructive() -> None:
    def tool(**hints) -> types.Tool:
        return types.Tool(
            name="t", input_schema={"type": "object"}, annotations=types.ToolAnnotations(**hints)
        )

    assert snapshots.is_destructive(tool(destructive_hint=True))
    assert not snapshots.is_destructive(tool(destructive_hint=True, read_only_hint=True))
    assert not snapshots.is_destructive(tool())
    assert not snapshots.is_destructive(types.Tool(name="t", input_schema={"type": "object"}))
