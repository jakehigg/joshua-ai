"""`/readyz` on core: what the body says, and what the status code says.

The status code is the part a probe reads. These tests are offline: the
database is a fake connection, and the data root is a bootstrapped temp tree.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from joshua_core.main import readyz
from joshua_core.memory import embed as embed_module
from joshua_shared import layout


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bootstrapped data volume, so the layout check has nothing to report."""
    root = tmp_path / "data"
    monkeypatch.setenv(layout.DATA_DIR_ENV, str(root))
    root.mkdir(parents=True)
    layout.bootstrap_wiki()
    layout.bootstrap_shared("Test")
    return root


class _Connection:
    async def execute(self, sql: str) -> None:
        return None


class _Pool:
    def __init__(self, *, up: bool):
        self._up = up

    @contextlib.asynccontextmanager
    async def connection(self):
        if not self._up:
            raise OSError("connection refused")
        yield _Connection()


def _request(*, db_up: bool):
    repo = SimpleNamespace(_pool=_Pool(up=db_up))
    ctx = SimpleNamespace(repo=repo)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=ctx)))


def _call(*, db_up: bool):
    response = asyncio.run(readyz(_request(db_up=db_up)))
    return response.status_code, json.loads(response.body)


def test_a_well_instance_answers_200(data_root: Path) -> None:
    status, body = _call(db_up=True)
    assert status == 200
    assert body["ok"] is True


def test_a_lost_database_answers_503(data_root: Path) -> None:
    """The probe could never fail before: every answer was a 200."""
    status, body = _call(db_up=False)
    assert status == 503
    assert body["ok"] is False
    assert body["checks"]["db"] is False


def test_a_broken_layout_answers_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(layout.DATA_DIR_ENV, str(tmp_path / "absent"))
    status, body = _call(db_up=True)
    assert status == 503
    assert body["checks"]["layout"] is False


def test_a_lost_embedding_model_keeps_the_200(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost model costs the memory and not the turn, so core stays in service."""
    monkeypatch.setattr(embed_module, "is_available", lambda: False)
    status, body = _call(db_up=True)
    assert status == 200
    assert body["ok"] is True
    assert body["checks"]["embed"] is False


def test_the_layout_check_leaves_no_probe_file(data_root: Path) -> None:
    _call(db_up=True)
    assert [p.name for p in data_root.iterdir() if p.name.startswith(".joshua-write-probe")] == []


def test_the_body_holds_no_names(data_root: Path) -> None:
    _, body = _call(db_up=True)
    assert set(body) == {"ok", "checks"}
    assert set(body["checks"]) == {"db", "layout", "embed"}
