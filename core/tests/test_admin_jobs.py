"""The `/admin/jobs/*` routes, with the app and a fake runner. No Postgres."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from jobs_fakes import TOPIC, FakeJobRunStore, FakeStore, build_wiki, fixed_clock, make_settings
from joshua_core.admin import build_admin_router
from joshua_core.jobs import BundleJob, JobArgsError, JobBusy, JobRunner, UnknownJob
from joshua_core.memory import embed as embed_module
from starlette.testclient import TestClient

LAPTOP = "laptop-token"
CORE = "core-token"
ON = "jobs:\n  enabled: true\n"


class FakeRunner:
    def __init__(self) -> None:
        self.started: list[tuple[str, dict[str, Any], bool]] = []
        self.busy = False
        self.raise_on_start: Exception | None = None
        self.runs = {
            7: {"id": 7, "job": "bundle", "report": {"summary": "seven"}},
            8: {"id": 8, "job": "other", "report": None},
        }
        self.listed: list[tuple[str | None, int]] = []

    def has(self, name: str) -> bool:
        return name == "bundle"

    async def start(self, job: str, args: dict[str, Any], dry_run: bool) -> int:
        if self.raise_on_start is not None:
            raise self.raise_on_start
        if job != "bundle":
            raise UnknownJob(job)
        self.started.append((job, args, dry_run))
        return 42

    async def get_run(self, run_id: int) -> dict[str, Any] | None:
        return self.runs.get(run_id)

    async def list_runs(self, job: str | None, limit: int) -> list[dict[str, Any]]:
        self.listed.append((job, limit))
        return [r for r in self.runs.values() if job is None or r["job"] == job][:limit]

    async def last_run(self, job: str) -> dict[str, Any] | None:
        return self.runs[7] if job == "bundle" else None


def _client(monkeypatch, extra: str = ON, runner: Any = None) -> TestClient:
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP)
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE)
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    app = FastAPI()
    app.state.ctx = SimpleNamespace(
        settings=make_settings(extra), jobs=runner if runner is not None else FakeRunner()
    )
    app.include_router(build_admin_router())
    tc = TestClient(app)
    tc.ctx = app.state.ctx  # type: ignore[attr-defined]
    return tc


def _laptop() -> dict[str, str]:
    return {"Authorization": f"Bearer {LAPTOP}"}


ROUTES = [
    ("post", "/admin/jobs/bundle/run"),
    ("get", "/admin/jobs/bundle/status"),
    ("get", "/admin/jobs/runs/7"),
    ("get", "/admin/jobs/runs"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_job_route_needs_an_admin_caller(monkeypatch, method: str, path: str) -> None:
    client = _client(monkeypatch)
    assert getattr(client, method)(path).status_code == 401
    core = {"Authorization": f"Bearer {CORE}"}
    assert getattr(client, method)(path, headers=core).status_code == 403
    assert client.ctx.jobs.started == []


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_job_route_is_404_when_jobs_are_off(monkeypatch, method: str, path: str) -> None:
    client = _client(monkeypatch, extra="")
    resp = getattr(client, method)(path, headers=_laptop())
    assert resp.status_code == 404
    assert "jobs.enabled" in resp.json()["error"]
    assert client.ctx.jobs.started == []


def test_a_core_with_no_runner_is_404(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.ctx.jobs = None
    assert client.get("/admin/jobs/runs", headers=_laptop()).status_code == 404


def test_run_answers_202_with_the_run_id(monkeypatch) -> None:
    client = _client(monkeypatch)
    resp = client.post(
        "/admin/jobs/bundle/run",
        headers=_laptop(),
        json={"args": {"topic": TOPIC}, "dry_run": False},
    )
    assert resp.status_code == 202
    assert resp.json() == {"run_id": 42, "job": "bundle", "dry_run": False}
    assert client.ctx.jobs.started == [("bundle", {"topic": TOPIC}, False)]


def test_run_is_a_dry_run_unless_the_body_says_otherwise(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert client.post("/admin/jobs/bundle/run", headers=_laptop()).status_code == 202
    assert client.post("/admin/jobs/bundle/run", headers=_laptop(), json={}).status_code == 202
    assert client.ctx.jobs.started == [("bundle", {}, True), ("bundle", {}, True)]


def test_run_of_an_unknown_job_is_404(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert client.post("/admin/jobs/nope/run", headers=_laptop(), json={}).status_code == 404


def test_run_while_the_lock_is_held_is_409(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.ctx.jobs.raise_on_start = JobBusy("bundle")
    resp = client.post("/admin/jobs/bundle/run", headers=_laptop(), json={})
    assert resp.status_code == 409


def test_run_with_bad_arguments_is_400(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.ctx.jobs.raise_on_start = JobArgsError("topic is required")
    resp = client.post("/admin/jobs/bundle/run", headers=_laptop(), json={})
    assert resp.status_code == 400
    assert resp.json()["error"] == "topic is required"


@pytest.mark.parametrize(
    "body",
    [
        ["not", "an", "object"],
        {"args": ["x"]},
        {"args": {}, "dry_run": "yes"},
    ],
)
def test_run_with_a_bad_body_is_400(monkeypatch, body: Any) -> None:
    client = _client(monkeypatch)
    resp = client.post("/admin/jobs/bundle/run", headers=_laptop(), json=body)
    assert resp.status_code == 400
    assert client.ctx.jobs.started == []


def test_run_with_a_body_that_is_not_json_is_400(monkeypatch) -> None:
    client = _client(monkeypatch)
    resp = client.post(
        "/admin/jobs/bundle/run",
        headers={**_laptop(), "Content-Type": "application/json"},
        content=b"{nope",
    )
    assert resp.status_code == 400


def test_status_reports_the_lock_and_the_last_run(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.ctx.jobs.busy = True
    resp = client.get("/admin/jobs/bundle/status", headers=_laptop())
    assert resp.status_code == 200
    assert resp.json() == {"job": "bundle", "busy": True, "last_run": client.ctx.jobs.runs[7]}
    assert client.get("/admin/jobs/nope/status", headers=_laptop()).status_code == 404


def test_one_run(monkeypatch) -> None:
    client = _client(monkeypatch)
    resp = client.get("/admin/jobs/runs/7", headers=_laptop())
    assert resp.status_code == 200 and resp.json()["report"] == {"summary": "seven"}
    assert client.get("/admin/jobs/runs/99", headers=_laptop()).status_code == 404
    assert client.get("/admin/jobs/runs/abc", headers=_laptop()).status_code == 400


def test_the_runs_list_filters_and_clamps(monkeypatch) -> None:
    client = _client(monkeypatch)
    resp = client.get("/admin/jobs/runs?job=bundle&limit=5000", headers=_laptop())
    assert resp.status_code == 200
    assert [r["id"] for r in resp.json()["runs"]] == [7]
    client.get("/admin/jobs/runs?limit=abc", headers=_laptop())
    client.get("/admin/jobs/runs?job=&limit=0", headers=_laptop())
    assert client.ctx.jobs.listed == [("bundle", 200), (None, 20), (None, 1)]


def test_a_dry_run_end_to_end_with_the_real_runner(monkeypatch, tmp_path) -> None:
    """The routes and the real runner, with the run table in memory."""
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [1.0] + [0.0] * 383)
    data_root = tmp_path / "data"
    build_wiki(data_root)
    settings = make_settings(ON)
    store = FakeJobRunStore()
    runner = JobRunner(
        repo=store,
        settings=settings,
        data_dir=data_root,
        jobs=[BundleJob(store=FakeStore(), settings=settings, data_dir=data_root)],
        lock=asyncio.Lock(),
        clock=fixed_clock(),
    )
    client = _client(monkeypatch, runner=runner)
    with client:
        bad = client.post(
            "/admin/jobs/bundle/run", headers=_laptop(), json={"args": {"topic": "nope.md"}}
        )
        assert bad.status_code == 400
        resp = client.post(
            "/admin/jobs/bundle/run", headers=_laptop(), json={"args": {"topic": TOPIC}}
        )
        assert resp.status_code == 202
        run_id = resp.json()["run_id"]
        for _ in range(100):
            run = client.get(f"/admin/jobs/runs/{run_id}", headers=_laptop()).json()
            if run["finished_at"] is not None:
                break
            time.sleep(0.02)
        assert run["report"]["data"]["paths"][0] == TOPIC
        assert run["dry_run"] is True
        status = client.get("/admin/jobs/bundle/status", headers=_laptop()).json()
        assert status["last_run"]["id"] == run_id
    assert not (data_root / "wiki" / "journal" / "2026" / "10" / "19").exists()
