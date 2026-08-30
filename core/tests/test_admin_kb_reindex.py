"""`POST /admin/kb/reindex` runs the pass, or starts it and answers at once.

A full re-embed of a large corpus takes minutes and holds a CPU for all of
them. The restore script must not wait for it, so the route takes
``background``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from joshua_core.admin import build_admin_router
from starlette.testclient import TestClient

LAPTOP = "laptop-token"


class FakeIndexer:
    """Counts passes and can block, so a test can prove the route did not wait."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.release = asyncio.Event()
        self.blocking = False

    async def reindex(self, *, source=None, person=None, full=False):
        self.calls.append({"source": source, "person": person, "full": full})
        if self.blocking:
            await self.release.wait()
        return {"files": {"indexed": 1}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP)
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    indexer = FakeIndexer()
    app = FastAPI()
    app.state.ctx = SimpleNamespace(indexer=indexer)
    app.include_router(build_admin_router())
    tc = TestClient(app)
    tc.indexer = indexer  # type: ignore[attr-defined]
    return tc


def _laptop() -> dict[str, str]:
    return {"Authorization": f"Bearer {LAPTOP}"}


def test_reindex_needs_a_bearer(client) -> None:
    assert client.post("/admin/kb/reindex", json={}).status_code == 401


def test_foreground_reindex_returns_the_results(client) -> None:
    response = client.post("/admin/kb/reindex", json={"full": True}, headers=_laptop())
    assert response.status_code == 200
    assert response.json() == {"results": {"files": {"indexed": 1}}}
    assert client.indexer.calls == [{"source": None, "person": None, "full": True}]


def test_background_reindex_answers_202_without_waiting(client) -> None:
    """The pass blocks forever. A background request must still answer."""
    client.indexer.blocking = True
    response = client.post(
        "/admin/kb/reindex", json={"full": True, "background": True}, headers=_laptop()
    )
    assert response.status_code == 202
    assert response.json() == {
        "started": True,
        "source": None,
        "person": None,
        "full": True,
    }


def test_background_reindex_still_runs_the_pass(client) -> None:
    response = client.post(
        "/admin/kb/reindex",
        json={"person": "alex", "background": True},
        headers=_laptop(),
    )
    assert response.status_code == 202
    # The task is scheduled on the loop the TestClient owns. A second request
    # that awaits gives the loop a turn, so the background pass runs.
    client.post("/admin/kb/reindex", json={}, headers=_laptop())
    assert {"source": None, "person": "alex", "full": False} in client.indexer.calls
