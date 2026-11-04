"""`POST /admin/inference/check`: who may call it, one call for each distinct
target, the outcome of each job, and no key in the answer or the log. The stub
call stands in for the model, so no test reaches a network."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from engine_fakes import BASE_CONFIG, FakeRepo
from fastapi import FastAPI
from joshua_core.admin import build_admin_router
from joshua_core.engine import agent
from joshua_core.engine import inference as inference_plan
from joshua_core.engine.stub_agent import StubOneshot
from joshua_shared import config as config_module
from joshua_shared.config import INFERENCE_JOBS
from starlette.testclient import TestClient

LAPTOP = "laptop-token"
CORE = "core-token"
OAUTH = "fake-oauth-token-0001"
GLOBAL_KEY = "fake-global-key-0002"
JOB_KEY = "fake-job-key-0003"
URL_PASSWORD = "fake-url-password-0004"
KEYS = (OAUTH, GLOBAL_KEY, JOB_KEY, URL_PASSWORD)
SECRET_ENV = {"INFERENCE_API_KEY": GLOBAL_KEY, "LOCAL_API_KEY": JOB_KEY}

# Every job on one server with one model: one target.
ONE_TARGET = """
inference:
  base_url: http://litellm:4000
  api_key: ${INFERENCE_API_KEY}
  chat:
    model: bedrock-claude-sonnet
  voice:
    model: bedrock-claude-sonnet
  reflection:
    model: bedrock-claude-sonnet
  describe:
    model: bedrock-claude-sonnet
  internet:
    model: bedrock-claude-sonnet
"""

# The same, but the internet job is on a second server: two targets.
TWO_TARGETS = """
inference:
  base_url: http://litellm:4000
  api_key: ${INFERENCE_API_KEY}
  chat:
    model: bedrock-claude-sonnet
  voice:
    model: bedrock-claude-sonnet
  reflection:
    model: bedrock-claude-sonnet
  describe:
    model: bedrock-claude-sonnet
  internet:
    base_url: http://ollama:11434
    api_key: ${LOCAL_API_KEY}
    model: bedrock-claude-sonnet
"""

# A server URL that holds a password.
URL_WITH_PASSWORD = f"""
inference:
  base_url: http://user:{URL_PASSWORD}@litellm:4000
  api_key: ""
"""


def settings(section: str = "") -> Any:
    return config_module.parse(BASE_CONFIG + section, env=SECRET_ENV, source="<test>")


@pytest.fixture(autouse=True)
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(inference_plan, "_oauth_token", OAUTH)


def make_client(monkeypatch: pytest.MonkeyPatch, section: str = "", oneshot: Any = None) -> Any:
    monkeypatch.setenv("JOSHUA_TOKEN_LAPTOP", LAPTOP)
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE)
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)
    app = FastAPI()
    app.state.ctx = SimpleNamespace(
        settings=settings(section),
        repo=FakeRepo(),
        oneshot=StubOneshot() if oneshot is None else oneshot,
    )
    app.include_router(build_admin_router())
    client = TestClient(app)
    client.ctx = app.state.ctx  # type: ignore[attr-defined]
    return client


def post(client: Any) -> Any:
    return client.post("/admin/inference/check", headers={"Authorization": f"Bearer {LAPTOP}"})


def logged_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(f"{r.msg!r} {r.getMessage()}" for r in caplog.records)


# ── who may call it ──────────────────────────────────────────────────────────


def test_the_check_refuses_a_caller_with_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    assert client.post("/admin/inference/check").status_code == 401
    assert client.ctx.oneshot.calls == []


def test_the_check_refuses_a_caller_that_is_not_an_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch)
    resp = client.post("/admin/inference/check", headers={"Authorization": f"Bearer {CORE}"})
    assert resp.status_code == 403
    assert client.ctx.oneshot.calls == []


# ── one call for each distinct target ────────────────────────────────────────


def test_jobs_with_one_server_model_and_key_share_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(monkeypatch, ONE_TARGET)
    body = post(client).json()

    assert len(client.ctx.oneshot.calls) == 1
    assert [j["job"] for j in body["jobs"]] == list(INFERENCE_JOBS)
    assert all(j["ok"] and j["endpoint"] == "litellm:4000" for j in body["jobs"])
    assert body["ok"] is True


def test_a_job_on_a_second_server_gets_its_own_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch, TWO_TARGETS)
    body = post(client).json()

    calls = client.ctx.oneshot.calls
    assert sorted(c.endpoint for c in calls) == ["litellm:4000", "ollama:11434"]
    internet = next(j for j in body["jobs"] if j["job"] == "internet")
    assert internet["endpoint"] == "ollama:11434"


def test_a_job_with_a_different_model_gets_its_own_call(monkeypatch: pytest.MonkeyPatch) -> None:
    section = ONE_TARGET.replace(
        "  describe:\n    model: bedrock-claude-sonnet", "  describe:\n    model: other"
    )
    client = make_client(monkeypatch, section)
    post(client)
    assert sorted(c.model for c in client.ctx.oneshot.calls) == ["bedrock-claude-sonnet", "other"]


def test_jobs_with_different_keys_on_one_server_get_their_own_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    section = ONE_TARGET.replace(
        "  chat:\n    model: bedrock-claude-sonnet",
        "  chat:\n    api_key: ${LOCAL_API_KEY}\n    model: bedrock-claude-sonnet",
    )
    client = make_client(monkeypatch, section)
    body = post(client).json()

    calls = client.ctx.oneshot.calls
    assert len(calls) == 2
    assert {c.endpoint for c in calls} == {"litellm:4000"}
    assert sorted(c.env["ANTHROPIC_AUTH_TOKEN"] for c in calls) == sorted([JOB_KEY, GLOBAL_KEY])
    assert no_key_in(body)


def test_each_call_writes_one_usage_row(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch, TWO_TARGETS)
    post(client)
    rows = client.ctx.repo.usage_events
    assert len(rows) == 2
    assert {r.endpoint for r in rows} == {"litellm:4000", "ollama:11434"}
    assert {r.kind for r in rows} <= {"chat", "reflection", "describe", "internet"}


# ── nothing set ──────────────────────────────────────────────────────────────


def test_with_nothing_set_every_job_checks_the_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(monkeypatch)
    body = post(client).json()

    assert {j["endpoint"] for j in body["jobs"]} == {"anthropic"}
    for call in client.ctx.oneshot.calls:
        assert call.env["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH
        assert call.env["ANTHROPIC_BASE_URL"] == ""
    assert no_key_in(body)


# ── a failed call ────────────────────────────────────────────────────────────


def no_key_in(value: Any) -> bool:
    text = json.dumps(value)
    return not any(key in text for key in KEYS)


def test_a_failed_server_gives_ok_false_and_a_short_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(monkeypatch, TWO_TARGETS)
    client.ctx.oneshot.fail["ollama:11434"] = ("http_404", "model 'x' not found")
    body = post(client).json()

    internet = next(j for j in body["jobs"] if j["job"] == "internet")
    chat = next(j for j in body["jobs"] if j["job"] == "chat")
    assert internet["ok"] is False
    assert internet["error"] == "http_404: model 'x' not found"
    assert chat["ok"] is True
    assert "error" not in chat
    assert body["ok"] is False
    assert isinstance(internet["seconds"], float)


def test_a_key_in_the_error_text_is_not_in_the_answer_or_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(monkeypatch, TWO_TARGETS)
    text = f"401 from upstream: key {JOB_KEY} and {GLOBAL_KEY} bad, token {OAUTH}"
    client.ctx.oneshot.fail["ollama:11434"] = ("http_401", text)
    with caplog.at_level(logging.DEBUG):
        body = post(client).json()

    internet = next(j for j in body["jobs"] if j["job"] == "internet")
    assert internet["error"].startswith("http_401: 401 from upstream")
    assert "[REDACTED]" in internet["error"]
    assert no_key_in(body)
    logged = logged_text(caplog)
    assert not any(key in logged for key in KEYS)
    assert "401 from upstream" not in logged


def test_a_key_in_an_exception_is_not_in_the_answer_or_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(monkeypatch, ONE_TARGET)
    client.ctx.oneshot.raise_for["litellm:4000"] = RuntimeError(f"cli died, key={GLOBAL_KEY}")
    with caplog.at_level(logging.DEBUG):
        body = post(client).json()

    assert all(j["ok"] is False for j in body["jobs"])
    assert body["jobs"][0]["error"] == "RuntimeError: cli died, key=[REDACTED]"
    assert no_key_in(body)
    assert not any(key in logged_text(caplog) for key in KEYS)


def test_a_password_in_the_server_url_is_not_in_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client(monkeypatch, URL_WITH_PASSWORD)
    client.ctx.oneshot.fail["litellm:4000"] = ("error", f"bad url user:{URL_PASSWORD}@litellm")
    body = post(client).json()
    assert no_key_in(body)
    assert all(j["endpoint"] == "litellm:4000" for j in body["jobs"])


def test_a_long_error_is_cut_short(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(monkeypatch, ONE_TARGET)
    client.ctx.oneshot.fail["litellm:4000"] = ("http_500", "x" * 5000)
    body = post(client).json()
    error = body["jobs"][0]["error"]
    assert len(error) == inference_plan.CHECK_ERROR_CHARS
    assert error.endswith("...")


def test_scrub_puts_the_text_on_one_line() -> None:
    assert inference_plan.scrub("a\n  b\tc", []) == "a b c"


# ── the time limit and the real call ─────────────────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.timeouts.append(kwargs["timeout_s"])
        return "ok"


async def test_the_limit_of_a_call_is_never_more_than_the_turn_limit() -> None:
    recorder = _Recorder()
    cfg = settings("inference:\n  timeout_seconds: 5\n")
    await inference_plan.check(cfg, oneshot=recorder)
    assert recorder.timeouts and set(recorder.timeouts) == {5.0}

    recorder = _Recorder()
    await inference_plan.check(settings(), oneshot=recorder)
    assert set(recorder.timeouts) == {inference_plan.CHECK_TIMEOUT_S}


def test_with_no_stub_the_route_uses_run_oneshot(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(agent, "run_oneshot", recorder)
    client = make_client(monkeypatch, ONE_TARGET, oneshot=False)
    body = post(client).json()
    assert body["ok"] is True
    assert len(recorder.timeouts) == 1
