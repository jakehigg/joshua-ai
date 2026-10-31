"""The `inference` section in core: the startup scrub, the job of each call,
the failed-call contract, the time limit, and the resume across a server
change. Fakes stand in for the SDK, the database, and the model."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from engine_fakes import (
    BASE_CONFIG,
    FakeComposer,
    FakeRepo,
    make_channel,
    make_conversation,
)
from joshua_core.engine import agent, describe, internet
from joshua_core.engine import inference as inference_plan
from joshua_core.engine.manager import ConversationManager
from joshua_core.engine.stub_agent import HANG_MARKER
from joshua_core.engine.tools.internet import do_lookup
from joshua_core.engine.types import FAILED_REPLY
from joshua_shared import config as config_module
from joshua_shared.attachments import AttachmentMeta, write_meta
from joshua_shared.inference import ENV_KEYS

OAUTH = "fake-oauth-token-0001"
GLOBAL_KEY = "fake-global-key-0002"
JOB_KEY = "fake-job-key-0003"
KEYS = (OAUTH, GLOBAL_KEY, JOB_KEY)
SECRET_ENV = {"INFERENCE_API_KEY": GLOBAL_KEY, "LITELLM_API_KEY": JOB_KEY}

# The global key on the Anthropic API, and the internet job on its own server.
SPLIT = """
inference:
  api_key: ${INFERENCE_API_KEY}
  internet:
    base_url: http://litellm:4000/v1
    api_key: ${LITELLM_API_KEY}
    model: local-model
"""

# Every job on one other server.
ELSEWHERE = """
inference:
  base_url: http://litellm:4000
  api_key: ${INFERENCE_API_KEY}
"""

VOICE = """
channels:
  voice:
    model: voice-model
"""

# The text of a failed call. It names a host and holds a key, the worst case.
ERROR_TEXT = f"API Error: 422 from http://litellm.internal:4000 key={JOB_KEY}"


def settings(section: str = "", extra: str = "") -> Any:
    return config_module.parse(BASE_CONFIG + section + extra, env=SECRET_ENV, source="<test>")


@pytest.fixture(autouse=True)
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with the subscription token kept, as after a start."""
    monkeypatch.setattr(inference_plan, "_oauth_token", OAUTH)


def no_key_in(values: Any) -> bool:
    text = repr(values)
    return not any(key in text for key in KEYS)


def logged_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(f"{r.msg!r} {r.getMessage()} {r.args!r}" for r in caplog.records)


# ── the startup scrub ────────────────────────────────────────────────────────


def test_take_credentials_keeps_the_token_and_removes_every_provider_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(inference_plan, "_oauth_token", "")
    env = dict.fromkeys(ENV_KEYS, "x")
    env["CLAUDE_CODE_OAUTH_TOKEN"] = OAUTH
    env["JOSHUA_TOKEN_CORE"] = "stays"

    removed = inference_plan.take_credentials(env)

    assert not set(ENV_KEYS) & set(env)
    assert env == {"JOSHUA_TOKEN_CORE": "stays"}
    assert sorted(removed) == sorted(ENV_KEYS)
    assert no_key_in(removed)
    chat = inference_plan.resolve(settings(), "chat")
    assert chat.env["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH


def test_after_the_scrub_no_provider_variable_is_in_os_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ENV_KEYS:
        monkeypatch.setenv(key, "inherited")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH)

    inference_plan.take_credentials()

    assert [key for key in ENV_KEYS if key in os.environ] == []
    assert inference_plan.resolve(settings(), "chat").env["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH


def test_a_scrub_with_no_token_gives_an_empty_one(monkeypatch: pytest.MonkeyPatch) -> None:
    env: dict[str, str] = {}
    assert inference_plan.take_credentials(env) == []
    assert inference_plan.resolve(settings(), "chat").env["CLAUDE_CODE_OAUTH_TOKEN"] == ""


def test_startup_takes_the_credentials_and_logs_the_names_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from joshua_core import main

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH)
    monkeypatch.setenv("ANTHROPIC_API_KEY", GLOBAL_KEY)
    with caplog.at_level("DEBUG"):
        main._take_credentials()

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "ANTHROPIC_API_KEY" in logged_text(caplog)
    assert no_key_in(logged_text(caplog))


# ── the plan at start ────────────────────────────────────────────────────────


def test_the_plan_names_a_job_key_on_the_global_server() -> None:
    cfg = settings(ELSEWHERE + "  chat:\n    api_key: ${LITELLM_API_KEY}\n")
    plan = inference_plan.plan(cfg)
    assert plan["chat"]["override"] == ["key"]
    assert plan["chat"]["endpoint"] == "litellm:4000"
    assert plan["voice"]["override"] == []
    assert no_key_in(plan)


def test_the_plan_names_each_server_model_and_override() -> None:
    plan = inference_plan.plan(settings(SPLIT, VOICE))

    assert plan["chat"] == {"endpoint": "anthropic", "model": "claude-opus-5", "override": []}
    assert plan["voice"]["model"] == "voice-model"
    assert plan["internet"] == {
        "endpoint": "litellm:4000",
        "model": "local-model",
        "override": ["server", "model"],
    }
    assert no_key_in(plan)


def test_the_plan_log_holds_no_key_and_warns_about_web_search(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("DEBUG"):
        inference_plan.log_plan(settings(SPLIT))

    infos = [
        r for r in caplog.records if isinstance(r.msg, dict) and r.msg["message"] == "inference"
    ]
    assert len(infos) == 1
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0].msg["endpoint"] == "litellm:4000"
    assert no_key_in(logged_text(caplog))


@pytest.mark.parametrize(
    "extra",
    ["", "\ninternet:\n  enabled: false\n"],
    ids=["internet on anthropic", "internet off"],
)
def test_no_web_search_warning_when_it_can_work(
    caplog: pytest.LogCaptureFixture, extra: str
) -> None:
    section = "" if not extra else ELSEWHERE
    with caplog.at_level("DEBUG"):
        inference_plan.log_plan(settings(section, extra))
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_the_turn_job_comes_from_the_profile() -> None:
    assert inference_plan.turn_job(SimpleNamespace(job="voice")) == "voice"
    assert inference_plan.turn_job(SimpleNamespace(name="dm")) == "chat"


# ── a fake SDK that records what it was built with ──────────────────────────


class Options:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class Result:
    def __init__(
        self,
        *,
        result: str | None = "ok",
        is_error: bool = False,
        structured: Any = None,
        status: int | None = None,
    ) -> None:
        self.result = result
        self.is_error = is_error
        self.structured_output = structured
        self.session_id = "sess-new"
        self.usage = {"input_tokens": 3, "output_tokens": 4}
        self.total_cost_usd = 9.99
        self.subtype = "success"
        self.api_error_status = status


ResultMessage = type("ResultMessage", (Result,), {})


class AssistantMessage:
    def __init__(self, text: str, error: str | None = None) -> None:
        self.content = [SimpleNamespace(text=text, name=None, type="text")]
        self.error = error


class Client:
    """Stands in for ``ClaudeSDKClient``. Each instance records its options."""

    built: list[Any] = []
    messages: list[Any] = []
    hang = False

    def __init__(self, options: Any) -> None:
        self.options = options
        self.interrupted = False
        Client.built.append(self)

    async def connect(self) -> None:
        return None

    async def get_mcp_status(self) -> dict[str, Any]:
        return {"mcpServers": []}

    async def query(self, prompt: Any) -> None:
        if not isinstance(prompt, str):
            async for _ in prompt:
                pass

    async def receive_response(self) -> Any:
        if Client.hang:
            await asyncio.Event().wait()
        for msg in Client.messages:
            yield msg

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        return None


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> type[Client]:
    monkeypatch.setattr(agent, "ClaudeAgentOptions", Options)
    monkeypatch.setattr(agent, "ClaudeSDKClient", Client)
    Client.built = []
    Client.messages = [ResultMessage(result="ok", structured={"ok": True})]
    Client.hang = False
    return Client


def sdk_manager(repo: FakeRepo, tmp_path: Path, section: str = "", **kw: Any) -> Any:
    return ConversationManager(
        repo=repo,
        settings=settings(section, kw.pop("extra", "")),
        composer=FakeComposer(),
        derive_profile=kw.pop(
            "derive_profile", lambda person, channel: SimpleNamespace(name="dm", max_turns=0)
        ),
        agent_backend=kw.pop("backend", "sdk"),
        data_dir=tmp_path,
        **kw,
    )


# ── each job gets its own key, and no other ──────────────────────────────────


async def test_the_chat_options_hold_the_global_key_and_no_job_key(
    sdk: type[Client], tmp_path: Path
) -> None:
    manager = sdk_manager(FakeRepo(), tmp_path, SPLIT)
    await manager.run_turn(make_channel(), make_conversation(), "hi")

    env = sdk.built[0].options.env
    assert set(env) == set(ENV_KEYS)
    assert env["ANTHROPIC_API_KEY"] == GLOBAL_KEY
    assert JOB_KEY not in env.values()
    assert OAUTH not in env.values()


async def test_the_internet_worker_holds_the_job_key_and_no_other(sdk: type[Client]) -> None:
    resolved = inference_plan.resolve(settings(SPLIT), "internet")
    await agent.run_worker_session(
        system_prompt="s",
        user_text="u",
        schema={},
        inference=resolved,
        timeout_s=5,
        max_turns=2,
        tools=["WebSearch"],
    )

    options = sdk.built[0].options
    assert options.env["ANTHROPIC_AUTH_TOKEN"] == JOB_KEY
    assert options.env["ANTHROPIC_BASE_URL"] == "http://litellm:4000"
    assert options.model == "local-model"
    assert GLOBAL_KEY not in options.env.values()
    assert OAUTH not in options.env.values()


async def test_the_describe_and_reflection_options_hold_no_job_key(sdk: type[Client]) -> None:
    cfg = settings(SPLIT)
    await agent.run_structured(
        system_prompt="s",
        user_text="u",
        schema={},
        inference=inference_plan.resolve(cfg, "describe"),
        timeout_s=5,
    )
    await agent.run_oneshot(
        system_prompt="s",
        user_prompt="u",
        inference=inference_plan.resolve(cfg, "reflection"),
        timeout_s=5,
    )

    for client in sdk.built:
        assert client.options.env["ANTHROPIC_API_KEY"] == GLOBAL_KEY
        assert JOB_KEY not in client.options.env.values()
        assert OAUTH not in client.options.env.values()


async def test_a_job_on_another_server_never_gets_the_subscription_token(
    sdk: type[Client], tmp_path: Path
) -> None:
    manager = sdk_manager(FakeRepo(), tmp_path, ELSEWHERE)
    await manager.run_turn(make_channel(), make_conversation(), "hi")

    env = sdk.built[0].options.env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert env["ANTHROPIC_AUTH_TOKEN"] == GLOBAL_KEY


# ── a failed call ────────────────────────────────────────────────────────────


async def test_a_failed_oneshot_gives_empty_text_and_logs_no_error_text(
    sdk: type[Client], caplog: pytest.LogCaptureFixture
) -> None:
    sdk.messages = [ResultMessage(result=ERROR_TEXT, is_error=True, status=422)]
    with caplog.at_level("DEBUG"):
        text = await agent.run_oneshot(
            system_prompt="s",
            user_prompt="u",
            inference=inference_plan.resolve(settings(SPLIT), "internet"),
            timeout_s=5,
        )

    assert text == ""
    failed = [r.msg for r in caplog.records if r.levelname == "WARNING"]
    assert failed == [
        {
            "message": "oneshot call failed",
            "error": "http_422",
            "job": "internet",
            "endpoint": "litellm:4000",
        }
    ]
    assert "litellm.internal" not in logged_text(caplog)
    assert no_key_in(logged_text(caplog))


@pytest.mark.parametrize("structured", [None, {"ok": True}])
async def test_a_failed_structured_call_gives_none(
    sdk: type[Client], caplog: pytest.LogCaptureFixture, structured: Any
) -> None:
    sdk.messages = [ResultMessage(result='{"ok": true}', is_error=True, structured=structured)]
    with caplog.at_level("DEBUG"):
        got = await agent.run_structured(
            system_prompt="s",
            user_text="u",
            schema={},
            inference=inference_plan.resolve(settings(), "describe"),
            timeout_s=5,
        )
    assert got is None
    assert any(
        isinstance(r.msg, dict) and r.msg["message"] == "worker call failed" for r in caplog.records
    )


async def test_a_failed_worker_session_gives_none(
    sdk: type[Client], caplog: pytest.LogCaptureFixture
) -> None:
    sdk.messages = [ResultMessage(result=ERROR_TEXT, is_error=True)]
    with caplog.at_level("DEBUG"):
        got = await agent.run_worker_session(
            system_prompt="s",
            user_text="u",
            schema={},
            inference=inference_plan.resolve(settings(SPLIT), "internet"),
            timeout_s=5,
            max_turns=2,
            tools=["WebSearch"],
        )
    assert got is None
    assert no_key_in(logged_text(caplog))
    assert "litellm.internal" not in logged_text(caplog)


async def test_the_error_message_of_a_failed_call_is_not_streamed(sdk: type[Client]) -> None:
    sdk.messages = [
        AssistantMessage(ERROR_TEXT, error="authentication_failed"),
        ResultMessage(result=ERROR_TEXT, is_error=True),
    ]
    options = agent.build_options(
        system_prompt="s",
        cwd=Path("/tmp"),
        mcp_servers={},
        model="m",
        env={},
        max_turns=1,
        resume=None,
    )
    deltas: list[str] = []

    async def on_delta(chunk: str) -> None:
        deltas.append(chunk)

    result = await agent.AgentSession(options).run("hi", on_delta)

    assert deltas == []
    assert result.is_error
    assert result.error == "authentication_failed"


async def test_a_turn_result_with_a_status_is_labelled_with_it(sdk: type[Client]) -> None:
    sdk.messages = [ResultMessage(result=ERROR_TEXT, is_error=True, status=401)]
    options = agent.build_options(
        system_prompt="s",
        cwd=Path("/tmp"),
        mcp_servers={},
        model="m",
        env={},
        max_turns=1,
        resume=None,
    )
    result = await agent.AgentSession(options).run("hi")
    assert result.error == "http_401"


async def test_the_error_text_of_a_chat_turn_never_reaches_the_person(
    sdk: type[Client], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sdk.messages = [
        AssistantMessage(ERROR_TEXT, error="invalid_request"),
        ResultMessage(result=ERROR_TEXT, is_error=True, status=422),
    ]
    repo = FakeRepo()
    manager = sdk_manager(repo, tmp_path, ELSEWHERE)
    deltas: list[str] = []

    async def on_delta(chunk: str) -> None:
        deltas.append(chunk)

    with caplog.at_level("DEBUG"):
        result = await manager.run_turn(
            make_channel(), make_conversation(), "hi", on_delta=on_delta
        )

    assert result.text == FAILED_REPLY
    assert deltas == []
    out = repo.transcripts[-1]
    assert (out["content"], out["status"]) == (FAILED_REPLY, "failed")
    assert out["meta"]["cost_usd"] is None
    assert "litellm.internal" not in logged_text(caplog)
    assert no_key_in(logged_text(caplog))
    assert no_key_in(repo.transcripts)


async def test_a_failed_stub_turn_gives_the_failure_reply(tmp_path: Path) -> None:
    repo = FakeRepo()
    manager = sdk_manager(repo, tmp_path, backend="stub")
    channel, conversation = make_channel(), make_conversation()
    await manager.run_turn(channel, conversation, "hello")
    manager._pool["c1"].session.fail_with = ERROR_TEXT

    result = await manager.run_turn(channel, conversation, "again")

    assert result.is_error
    assert result.text == FAILED_REPLY
    assert repo.transcripts[-1]["content"] == FAILED_REPLY
    assert no_key_in(repo.transcripts)


# ── the time limit ───────────────────────────────────────────────────────────

FAST = "\ninference:\n  timeout_seconds: 0.05\n"


async def test_a_turn_that_passes_the_limit_is_interrupted_and_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = FakeRepo()
    manager = sdk_manager(repo, tmp_path, FAST, backend="stub")
    channel, conversation = make_channel(), make_conversation()

    with caplog.at_level("DEBUG"):
        result = await manager.run_turn(channel, conversation, f"wait {HANG_MARKER}")

    mc = manager._pool["c1"]
    old = mc.session
    assert result.is_error
    assert result.error == "timeout"
    assert result.text == FAILED_REPLY
    assert old.interrupted
    assert not old.connected
    assert mc.replace_pending
    assert repo.transcripts[-1]["status"] == "failed"
    assert any(
        isinstance(r.msg, dict) and r.msg["message"] == "turn timed out" for r in caplog.records
    )

    after = await manager.run_turn(channel, conversation, "hello again")
    assert not after.is_error
    assert manager._pool["c1"].session is not old


async def test_a_failed_oneshot_gives_the_label_and_the_text_to_error_out(
    sdk: type[Client],
) -> None:
    sdk.messages = [ResultMessage(result=ERROR_TEXT, is_error=True, status=422)]
    errors: list[tuple[str, str]] = []
    text = await agent.run_oneshot(
        system_prompt="s",
        user_prompt="u",
        inference=inference_plan.resolve(settings(SPLIT), "internet"),
        timeout_s=5,
        error_out=errors,
    )
    assert text == ""
    assert errors == [("http_422", ERROR_TEXT)]


async def test_a_good_oneshot_gives_nothing_to_error_out(sdk: type[Client]) -> None:
    errors: list[tuple[str, str]] = []
    await agent.run_oneshot(
        system_prompt="s",
        user_prompt="u",
        inference=inference_plan.resolve(settings(), "reflection"),
        timeout_s=5,
        error_out=errors,
    )
    assert errors == []


async def test_a_oneshot_that_passes_the_limit_gives_timeout_to_error_out(
    sdk: type[Client],
) -> None:
    sdk.hang = True
    errors: list[tuple[str, str]] = []
    await agent.run_oneshot(
        system_prompt="s",
        user_prompt="u",
        inference=inference_plan.resolve(settings(), "reflection"),
        timeout_s=0.05,
        error_out=errors,
    )
    assert errors == [("timeout", "")]


async def test_a_oneshot_that_passes_the_limit_gives_empty_text(sdk: type[Client]) -> None:
    sdk.hang = True
    text = await agent.run_oneshot(
        system_prompt="s",
        user_prompt="u",
        inference=inference_plan.resolve(settings(), "reflection"),
        timeout_s=0.05,
    )
    assert text == ""
    assert sdk.built[0].interrupted


# ── resume across a server change ────────────────────────────────────────────


async def test_a_session_on_the_same_server_is_resumed(tmp_path: Path) -> None:
    manager = sdk_manager(FakeRepo(), tmp_path, backend="stub")
    for stored in (None, "anthropic"):
        conversation = make_conversation(sdk_session_id="old", inference_endpoint=stored)
        built = await manager._build_session(make_channel(), conversation, "old")
        assert built.session.resume == "old"


async def test_a_server_change_starts_a_new_session(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = FakeRepo()
    manager = sdk_manager(repo, tmp_path, ELSEWHERE, backend="stub")
    conversation = make_conversation(sdk_session_id="old")

    with caplog.at_level("DEBUG"):
        built = await manager._build_session(make_channel(), conversation, "old")
        result = await manager.run_turn(make_channel(), conversation, "hi")

    assert built.session.resume is None
    assert not result.is_error
    changes = [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg["message"] == "inference server changed; new session"
    ]
    assert changes and changes[0]["from"] == "anthropic" and changes[0]["to"] == "litellm:4000"
    assert not any(
        isinstance(r.msg, dict) and r.msg["message"].startswith("first turn failed")
        for r in caplog.records
    )
    assert repo.sdk_endpoints["c1"] == "litellm:4000"


async def test_a_session_from_another_server_is_not_resumed_on_anthropic(tmp_path: Path) -> None:
    manager = sdk_manager(FakeRepo(), tmp_path, backend="stub")
    conversation = make_conversation(sdk_session_id="old", inference_endpoint="litellm:4000")
    built = await manager._build_session(make_channel(), conversation, "old")
    assert built.session.resume is None


async def test_the_sdk_backend_does_not_pass_a_resume_from_another_server(
    sdk: type[Client], tmp_path: Path
) -> None:
    manager = sdk_manager(FakeRepo(), tmp_path, ELSEWHERE)
    conversation = make_conversation(sdk_session_id="old", inference_endpoint="anthropic")
    await manager.run_turn(make_channel(), conversation, "hi")
    assert not hasattr(sdk.built[0].options, "resume")


# ── the stub records the routing ─────────────────────────────────────────────


async def test_a_voice_profile_runs_the_voice_job(tmp_path: Path) -> None:
    manager = sdk_manager(
        FakeRepo(),
        tmp_path,
        "\ninference:\n  voice:\n    base_url: http://voice-llm:4000\n    api_key: ''\n",
        extra=VOICE,
        backend="stub",
        derive_profile=lambda person, channel: SimpleNamespace(
            name="voice-dm", job="voice", max_turns=0
        ),
    )
    await manager.run_turn(make_channel(), make_conversation(), "hi")

    session = manager._pool["c1"].session
    assert session.endpoint == "voice-llm:4000"
    assert session.model == "voice-model"
    assert session.inference.job == "voice"


async def test_a_text_profile_runs_the_chat_job(tmp_path: Path) -> None:
    manager = sdk_manager(FakeRepo(), tmp_path, SPLIT, backend="stub")
    await manager.run_turn(make_channel(), make_conversation(), "hi")

    session = manager._pool["c1"].session
    assert (session.inference.job, session.endpoint) == ("chat", "anthropic")


# ── usage and cost ───────────────────────────────────────────────────────────


async def test_a_chat_row_names_the_model_and_server_used(tmp_path: Path) -> None:
    repo = FakeRepo()
    manager = sdk_manager(
        repo, tmp_path, ELSEWHERE + "  chat:\n    model: local-chat\n", backend="stub"
    )
    await manager.run_turn(make_channel(), make_conversation(), "hi")

    (row,) = repo.usage_events
    assert (row.kind, row.model, row.endpoint) == ("chat", "local-chat", "litellm:4000")
    assert repo.transcripts[-1]["meta"]["cost_usd"] is None


async def test_a_chat_cost_is_kept_on_anthropic(sdk: type[Client], tmp_path: Path) -> None:
    repo = FakeRepo()
    manager = sdk_manager(repo, tmp_path)
    await manager.run_turn(make_channel(), make_conversation(), "hi")

    assert repo.transcripts[-1]["meta"]["cost_usd"] == 9.99
    assert repo.usage_events[0].endpoint == "anthropic"


async def test_a_described_file_uses_the_describe_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    async def fake(path: Path, **kwargs: Any) -> Any:
        seen.append(kwargs["inference"])
        kwargs["usage_out"].append(internet.Usage(input=2))
        return None

    monkeypatch.setattr("joshua_core.engine.manager.describe_attachment", fake)
    rel = "people/alex/attachments/2026/09/2026-09-16-140509-IMG.jpg"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xd8\xff")
    write_meta(path, AttachmentMeta(mime="image/jpeg"))
    repo = FakeRepo()
    manager = sdk_manager(
        repo,
        tmp_path,
        "\ninference:\n  describe:\n    base_url: http://vision:4000\n    api_key: ''\n"
        "    model: local-vision\n",
        backend="stub",
    )
    from joshua_core.engine.types import Attachment

    await manager.run_turn(
        make_channel(),
        make_conversation(),
        "look",
        attachments=[Attachment(path=rel, mime="image/jpeg", name="IMG.jpg")],
    )

    assert [(r.job, r.model, r.endpoint) for r in seen] == [
        ("describe", "local-vision", "vision:4000")
    ]
    (row,) = [e for e in repo.usage_events if e.kind == "describe"]
    assert (row.model, row.endpoint) == ("local-vision", "vision:4000")


async def test_the_describer_writes_the_model_it_used(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(bytes.fromhex("89504e470d0a1a0a"))
    write_meta(path, AttachmentMeta(mime="image/png"))

    async def run(**kwargs: Any) -> Any:
        return {"kind": "photo", "subject": "a cat", "slug": "a-cat"}

    resolved = inference_plan.resolve(
        settings("\ninference:\n  describe:\n    model: local-vision\n"), "describe"
    )
    meta = await describe.describe_attachment(
        path, mime="image/png", timeout_s=5, inference=resolved, runner=run
    )
    assert meta is not None and meta.description is not None
    assert meta.description.model == "local-vision"


async def test_an_internet_lookup_uses_the_internet_job(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake(question: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        kwargs["usage_out"].append(internet.Usage(input=1))
        return None

    monkeypatch.setattr(internet, "ask", fake)
    repo = FakeRepo()
    deps = SimpleNamespace(repo=repo, person_id="alex", conversation=SimpleNamespace(id="c1"))

    await do_lookup(deps, settings=settings(SPLIT), question="q")  # type: ignore[arg-type]

    assert seen["inference"].job == "internet"
    assert seen["inference"].endpoint == "litellm:4000"
    (row,) = repo.usage_events
    assert (row.model, row.endpoint) == ("local-model", "litellm:4000")
