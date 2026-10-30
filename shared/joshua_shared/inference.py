"""Resolve the model, the server, and the CLI environment for one model job.

``resolve_inference`` reads the ``inference`` section of the config and does no
I/O. The Claude Agent SDK starts the CLI with ``{**os.environ, **options.env}``,
so ``options.env`` can replace an inherited variable but cannot remove one, and
the CLI reads an empty value as not set. So the environment that this module
gives holds every key in ``ENV_KEYS``, with ``""`` for each key that the job
does not use. Then no variable that the container inherits can change the
server or the credential of a job.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from joshua_shared.config import INFERENCE_JOBS, JoshuaConfig

# The variables that select a credential. At most one has a value.
AUTH_KEYS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)

# Every variable that selects a credential or a provider.
PROVIDER_KEYS = (
    *AUTH_KEYS,
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_GATEWAY",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_UNIX_SOCKET",
)

# The model tiers. On another server, every tier except ANTHROPIC_MODEL is the
# job model, so a call that the CLI makes on its own uses a model that exists
# there. The SDK passes the job model itself.
MODEL_TIER_KEYS = (
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
MODEL_KEYS = ("ANTHROPIC_MODEL", *MODEL_TIER_KEYS)

NONESSENTIAL_TRAFFIC_KEY = "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"

# The full set. Each environment from ``resolve_inference`` holds exactly these
# keys, and a caller can remove them from ``os.environ`` with the same list.
ENV_KEYS = (*PROVIDER_KEYS, *MODEL_KEYS, NONESSENTIAL_TRAFFIC_KEY)

ANTHROPIC_ENDPOINT = "anthropic"


@dataclass(frozen=True)
class ResolvedInference:
    """The model, the CLI environment, and the server label for one job.

    ``endpoint`` is safe to log: it is the host of the server, or
    ``"anthropic"``. It never holds a key or a path.
    """

    job: str
    model: str
    env: dict[str, str]
    endpoint: str


def job_model_setting(config: JoshuaConfig, job: str) -> str:
    """Return the model setting of ``job`` outside the ``inference`` section."""
    if job == "chat":
        return config.core.model
    if job == "voice":
        voice = config.channels.voice
        return voice.model if voice is not None else config.core.model
    if job == "reflection":
        return config.core.reflection_model
    if job == "describe":
        return config.attachments.describe.model
    if job == "internet":
        return config.internet.model
    raise KeyError(f"unknown inference job '{job}'; valid: {INFERENCE_JOBS}")


def cli_base_url(base_url: str) -> str:
    """Return ``base_url`` with no trailing ``/`` and no trailing ``/v1``.

    The CLI adds ``/v1/messages`` itself.
    """
    url = base_url.rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")].rstrip("/")
    return url


def endpoint_label(base_url: str | None) -> str:
    """Return the host (and port) of ``base_url``, or ``"anthropic"``."""
    if not base_url:
        return ANTHROPIC_ENDPOINT
    parts = urlsplit(base_url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{host}:{parts.port}" if parts.port is not None else host


def resolve_inference(config: JoshuaConfig, job: str, *, oauth_token: str) -> ResolvedInference:
    """Return the model, the CLI environment, and the server label for ``job``.

    The model is ``inference.<job>.model``, or else the model setting of the
    job. The server is the job's ``base_url`` and ``api_key`` when the job sets
    ``base_url``, or else the global ones. ``oauth_token`` is the subscription
    token, and it is used only when there is no ``base_url`` and no
    ``api_key``. Raises ``KeyError`` for an unknown job.
    """
    inference = config.inference
    block = inference.job(job)
    model = block.model or job_model_setting(config, job)
    if block.base_url is not None:
        base_url: str | None = block.base_url
        api_key = block.api_key or ""
    else:
        base_url = inference.base_url
        api_key = inference.api_key or ""

    env = dict.fromkeys(ENV_KEYS, "")
    if base_url is not None:
        env["ANTHROPIC_BASE_URL"] = cli_base_url(base_url)
        env["ANTHROPIC_AUTH_TOKEN"] = api_key
        for key in MODEL_TIER_KEYS:
            env[key] = model
        env[NONESSENTIAL_TRAFFIC_KEY] = "1"
    elif api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    else:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

    set_auth = [key for key in AUTH_KEYS if env[key]]
    if len(set_auth) > 1:
        raise AssertionError(f"more than one credential variable is set: {set_auth}")
    return ResolvedInference(job=job, model=model, env=env, endpoint=endpoint_label(base_url))
