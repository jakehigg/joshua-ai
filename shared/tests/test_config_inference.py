import pytest
from joshua_shared import config
from joshua_shared.inference import (
    AUTH_KEYS,
    ENV_KEYS,
    MODEL_TIER_KEYS,
    cli_base_url,
    endpoint_label,
    job_model_setting,
    resolve_inference,
)

BASE = """
name: Home
timezone: America/New_York
people:
  - id: alex
    name: Alex
    handles:
      telegram: "998877"
core:
  model: chat-model
  reflection_model: reflection-model
attachments:
  describe:
    model: describe-model
internet:
  model: internet-model
"""

VOICE = """
channels:
  voice:
    model: voice-model
"""

OAUTH = "oauth-token-value"
GLOBAL_KEY = "global-key-value"
JOB_KEY = "job-key-value"
ENV = {"INFERENCE_API_KEY": GLOBAL_KEY, "JOB_API_KEY": JOB_KEY}

JOB_MODELS = {
    "chat": "chat-model",
    "voice": "voice-model",
    "reflection": "reflection-model",
    "describe": "describe-model",
    "internet": "internet-model",
}


def load(section: str = "", *, voice: bool = True, env: dict[str, str] | None = None):
    text = BASE + (VOICE if voice else "") + section
    return config.parse(text, ENV if env is None else env, source="test.yaml")


def expected_env(**values: str) -> dict[str, str]:
    env = dict.fromkeys(ENV_KEYS, "")
    env.update(values)
    return env


def test_job_list_matches_the_inference_blocks() -> None:
    assert config.INFERENCE_JOBS == ("chat", "voice", "reflection", "describe", "internet")
    for job in config.INFERENCE_JOBS:
        assert job in config.Inference.model_fields


def test_no_section_is_the_subscription() -> None:
    cfg = load()
    for job in config.INFERENCE_JOBS:
        resolved = resolve_inference(cfg, job, oauth_token=OAUTH)
        assert resolved.env == expected_env(CLAUDE_CODE_OAUTH_TOKEN=OAUTH)
        assert resolved.model == JOB_MODELS[job]
        assert resolved.endpoint == "anthropic"
        assert resolved.job == job


def test_empty_values_from_unset_variables_are_the_subscription() -> None:
    cfg = load(
        "\ninference:\n  base_url: ${INFERENCE_BASE_URL:-}\n  api_key: ${NOPE:-}\n",
        env={},
    )
    resolved = resolve_inference(cfg, "chat", oauth_token=OAUTH)
    assert resolved.env == expected_env(CLAUDE_CODE_OAUTH_TOKEN=OAUTH)


def test_api_key_alone_is_the_anthropic_api() -> None:
    cfg = load("\ninference:\n  api_key: ${INFERENCE_API_KEY}\n")
    for job in config.INFERENCE_JOBS:
        resolved = resolve_inference(cfg, job, oauth_token=OAUTH)
        assert resolved.env == expected_env(ANTHROPIC_API_KEY=GLOBAL_KEY)
        assert resolved.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert resolved.endpoint == "anthropic"
        assert resolved.model == JOB_MODELS[job]


def test_global_base_url_is_inherited_by_every_job() -> None:
    cfg = load("\ninference:\n  base_url: http://litellm:4000\n  api_key: ${INFERENCE_API_KEY}\n")
    for job in config.INFERENCE_JOBS:
        resolved = resolve_inference(cfg, job, oauth_token=OAUTH)
        model = JOB_MODELS[job]
        assert resolved.model == model
        assert resolved.endpoint == "litellm:4000"
        assert resolved.env == expected_env(
            ANTHROPIC_BASE_URL="http://litellm:4000",
            ANTHROPIC_AUTH_TOKEN=GLOBAL_KEY,
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            **dict.fromkeys(MODEL_TIER_KEYS, model),
        )
        assert resolved.env["ANTHROPIC_MODEL"] == ""


def test_model_only_override_keeps_the_global_server() -> None:
    cfg = load(
        "\ninference:\n  base_url: http://litellm:4000\n  api_key: ${INFERENCE_API_KEY}\n"
        "  describe:\n    model: local-vision\n"
    )
    resolved = resolve_inference(cfg, "describe", oauth_token=OAUTH)
    assert resolved.model == "local-vision"
    assert resolved.endpoint == "litellm:4000"
    assert resolved.env["ANTHROPIC_AUTH_TOKEN"] == GLOBAL_KEY
    assert resolved.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "local-vision"
    assert resolve_inference(cfg, "chat", oauth_token=OAUTH).model == "chat-model"


def test_model_only_override_on_the_subscription() -> None:
    cfg = load("\ninference:\n  chat:\n    model: other-model\n")
    resolved = resolve_inference(cfg, "chat", oauth_token=OAUTH)
    assert resolved.model == "other-model"
    assert resolved.env == expected_env(CLAUDE_CODE_OAUTH_TOKEN=OAUTH)


def test_full_override_uses_the_job_server_and_key() -> None:
    cfg = load(
        "\ninference:\n  api_key: ${INFERENCE_API_KEY}\n"
        "  internet:\n    base_url: https://llm.example/v1/\n"
        "    api_key: ${JOB_API_KEY}\n    model: local-model\n"
    )
    resolved = resolve_inference(cfg, "internet", oauth_token=OAUTH)
    assert resolved.model == "local-model"
    assert resolved.endpoint == "llm.example"
    assert resolved.env == expected_env(
        ANTHROPIC_BASE_URL="https://llm.example",
        ANTHROPIC_AUTH_TOKEN=JOB_KEY,
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        **dict.fromkeys(MODEL_TIER_KEYS, "local-model"),
    )
    # The other jobs keep the global setting.
    chat = resolve_inference(cfg, "chat", oauth_token=OAUTH)
    assert chat.env == expected_env(ANTHROPIC_API_KEY=GLOBAL_KEY)


def test_job_env_never_holds_another_key() -> None:
    cfg = load(
        "\ninference:\n  base_url: http://litellm:4000\n  api_key: ${INFERENCE_API_KEY}\n"
        "  internet:\n    base_url: https://llm.example\n    api_key: ${JOB_API_KEY}\n"
        '  voice:\n    base_url: http://ollama:11434\n    api_key: ""\n'
    )
    internet = resolve_inference(cfg, "internet", oauth_token=OAUTH)
    voice = resolve_inference(cfg, "voice", oauth_token=OAUTH)
    chat = resolve_inference(cfg, "chat", oauth_token=OAUTH)
    for resolved, foreign in (
        (internet, (GLOBAL_KEY, OAUTH)),
        (voice, (GLOBAL_KEY, JOB_KEY, OAUTH)),
        (chat, (JOB_KEY, OAUTH)),
    ):
        for value in resolved.env.values():
            for key in foreign:
                assert key not in value
    assert voice.env["ANTHROPIC_AUTH_TOKEN"] == ""
    assert voice.endpoint == "ollama:11434"


def test_subscription_job_never_holds_a_job_key() -> None:
    cfg = load(
        "\ninference:\n  internet:\n"
        "    base_url: http://litellm:4000\n    api_key: ${JOB_API_KEY}\n"
    )
    chat = resolve_inference(cfg, "chat", oauth_token=OAUTH)
    assert JOB_KEY not in chat.env.values()
    assert chat.env == expected_env(CLAUDE_CODE_OAUTH_TOKEN=OAUTH)
    internet = resolve_inference(cfg, "internet", oauth_token=OAUTH)
    assert OAUTH not in internet.env.values()


def test_at_most_one_auth_variable_is_set() -> None:
    sections = (
        "",
        "\ninference:\n  api_key: ${INFERENCE_API_KEY}\n",
        "\ninference:\n  base_url: http://litellm:4000\n  api_key: ${INFERENCE_API_KEY}\n",
        '\ninference:\n  base_url: http://litellm:4000\n  api_key: ""\n',
    )
    for section in sections:
        cfg = load(section)
        for job in config.INFERENCE_JOBS:
            env = resolve_inference(cfg, job, oauth_token=OAUTH).env
            assert set(env) == set(ENV_KEYS)
            assert sum(1 for key in AUTH_KEYS if env[key]) <= 1


def test_env_has_exactly_the_full_key_set() -> None:
    assert len(set(ENV_KEYS)) == len(ENV_KEYS) == 24
    cfg = load('\ninference:\n  internet:\n    base_url: http://litellm:4000\n    api_key: ""\n')
    for job in config.INFERENCE_JOBS:
        assert set(resolve_inference(cfg, job, oauth_token=OAUTH).env) == set(ENV_KEYS)


def test_job_base_url_without_api_key_is_refused() -> None:
    with pytest.raises(config.ConfigError, match="global key would go to another server"):
        load(
            "\ninference:\n  api_key: ${INFERENCE_API_KEY}\n"
            "  internet:\n    base_url: http://litellm:4000\n"
        )


def test_job_api_key_without_base_url_is_refused() -> None:
    with pytest.raises(config.ConfigError, match="must also set base_url"):
        load("\ninference:\n  chat:\n    api_key: ${JOB_API_KEY}\n")


def test_empty_api_key_is_an_empty_string() -> None:
    cfg = load('\ninference:\n  chat:\n    base_url: http://ollama:11434\n    api_key: ""\n')
    assert cfg.inference.chat.api_key == ""
    bare = load("\ninference:\n  chat:\n    base_url: http://ollama:11434\n    api_key:\n")
    assert bare.inference.chat.api_key == ""


def test_unknown_job_key_is_refused() -> None:
    with pytest.raises(config.ConfigError, match="inference.research"):
        load("\ninference:\n  research:\n    model: x\n")
    with pytest.raises(config.ConfigError, match="inference.chat.timeout"):
        load("\ninference:\n  chat:\n    timeout: 3\n")


def test_unknown_job_name_is_refused_by_the_resolver() -> None:
    cfg = load()
    with pytest.raises(KeyError):
        resolve_inference(cfg, "research", oauth_token=OAUTH)
    with pytest.raises(KeyError):
        job_model_setting(cfg, "research")


@pytest.mark.parametrize(
    "url",
    ["litellm:4000", "ftp://llm.example", "http://", "not a url", "http://llm.example:port"],
)
def test_bad_url_is_refused(url: str) -> None:
    with pytest.raises(config.ConfigError, match="base_url"):
        load(f"\ninference:\n  base_url: {url}\n")
    with pytest.raises(config.ConfigError, match="base_url"):
        load(f'\ninference:\n  chat:\n    base_url: {url}\n    api_key: ""\n')


def test_literal_key_is_refused() -> None:
    with pytest.raises(config.ConfigError, match="inference.api_key"):
        load("\ninference:\n  api_key: sk-ant-literal123\n")
    with pytest.raises(config.ConfigError, match="inference.internet.api_key"):
        load(
            "\ninference:\n  internet:\n    base_url: http://litellm:4000\n"
            "    api_key: sk-ant-literal123\n"
        )


def test_key_from_the_environment_is_accepted() -> None:
    cfg = load(
        "\ninference:\n  api_key: ${INFERENCE_API_KEY}\n", env={"INFERENCE_API_KEY": "sk-ant-x1"}
    )
    assert cfg.inference.api_key == "sk-ant-x1"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://litellm:4000", "http://litellm:4000"),
        ("http://litellm:4000/", "http://litellm:4000"),
        ("http://litellm:4000/v1", "http://litellm:4000"),
        ("http://litellm:4000/v1/", "http://litellm:4000"),
        ("https://llm.example/proxy/v1", "https://llm.example/proxy"),
        ("https://llm.example/v10", "https://llm.example/v10"),
    ],
)
def test_cli_base_url_strips_v1(url: str, expected: str) -> None:
    assert cli_base_url(url) == expected


def test_resolved_base_url_strips_v1() -> None:
    cfg = load('\ninference:\n  base_url: http://litellm:4000/v1/\n  api_key: ""\n')
    env = resolve_inference(cfg, "chat", oauth_token=OAUTH).env
    assert env["ANTHROPIC_BASE_URL"] == "http://litellm:4000"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (None, "anthropic"),
        ("", "anthropic"),
        ("http://litellm:4000", "litellm:4000"),
        ("https://user:secret-key@llm.example/v1?key=secret-key", "llm.example"),
        ("https://llm.example:8443/a/b", "llm.example:8443"),
        ("http://[2001:db8::1]:4000/v1", "[2001:db8::1]:4000"),
    ],
)
def test_endpoint_label_is_the_host_only(url: str | None, expected: str) -> None:
    label = endpoint_label(url)
    assert label == expected
    assert "secret" not in label
    assert "/" not in label


def test_endpoint_of_a_url_with_userinfo_has_no_key() -> None:
    cfg = load(
        '\ninference:\n  base_url: "https://user:${JOB_API_KEY}@llm.example/v1"\n'
        "  api_key: ${INFERENCE_API_KEY}\n"
    )
    resolved = resolve_inference(cfg, "chat", oauth_token=OAUTH)
    assert resolved.endpoint == "llm.example"
    assert JOB_KEY not in resolved.endpoint
    assert GLOBAL_KEY not in resolved.endpoint


def test_voice_with_no_voice_section_uses_the_core_model() -> None:
    cfg = load(voice=False)
    assert cfg.channels.voice is None
    resolved = resolve_inference(cfg, "voice", oauth_token=OAUTH)
    assert resolved.model == "chat-model"


def test_empty_job_block_is_accepted() -> None:
    cfg = load('\ninference:\n  chat:\n  voice:\n    model: ""\n')
    assert cfg.inference.chat == config.InferenceJob()
    assert resolve_inference(cfg, "voice", oauth_token=OAUTH).model == "voice-model"


def test_inference_job_lookup() -> None:
    cfg = load()
    assert cfg.inference.job("chat") is cfg.inference.chat
    with pytest.raises(KeyError):
        cfg.inference.job("research")


def test_resolved_inference_is_frozen() -> None:
    resolved = resolve_inference(load(), "chat", oauth_token=OAUTH)
    with pytest.raises(AttributeError):
        resolved.model = "other"  # type: ignore[misc]


def test_timeout_seconds_default_is_600() -> None:
    assert load().inference.timeout_seconds == 600


def test_timeout_seconds_is_read() -> None:
    assert load("\ninference:\n  timeout_seconds: 90\n").inference.timeout_seconds == 90


@pytest.mark.parametrize("value", ["0", "-5"])
def test_timeout_seconds_must_be_more_than_zero(value: str) -> None:
    with pytest.raises(config.ConfigError):
        load(f"\ninference:\n  timeout_seconds: {value}\n")
