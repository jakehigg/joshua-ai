"""Resolve the model, the server, and the CLI environment for one model job.

``resolve_inference`` reads the ``inference`` section of the config and does no
I/O. The Claude Agent SDK starts the CLI with ``{**os.environ, **options.env}``,
so ``options.env`` can replace an inherited variable but cannot remove one, and
the CLI reads an empty value as not set. So the environment that this module
gives holds every key in ``ENV_KEYS``, with ``""`` for each key that the job
does not use. Then no variable that the container inherits can change the
server or the credential of a job.

``ENABLE_TOOL_SEARCH`` is in ``ENV_KEYS`` for the same reason. It is ``"true"``
only for a conversation job with tool search on, and ``""`` for every other
job, so an inherited value cannot turn tool search on or off.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from joshua_shared.config import CONVERSATION_JOBS, INFERENCE_JOBS, JoshuaConfig

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

# Tool search: the CLI sends the MCP tool definitions to the model only when
# the model asks for them with the CLI tool ``ToolSearch``. The CLI needs both
# this variable and ``ToolSearch`` in the tool list. On a server that is not
# Anthropic, the CLI keeps tool search off unless this variable is set.
TOOL_SEARCH_KEY = "ENABLE_TOOL_SEARCH"
TOOL_SEARCH_ON = "true"

# The tool search setting of each conversation job when its block sets none.
# A voice turn must answer fast, and tool search adds one model call.
TOOL_SEARCH_DEFAULTS = {"chat": True, "voice": False}

# Prompt cache lifetime. On the subscription the CLI writes the prompt cache
# for one hour. On any other server it writes it for five minutes, so a reply
# that comes more than five minutes after the last one writes the full prompt
# to the cache again. ``ENABLE_PROMPT_CACHING_1H`` makes the CLI ask for the
# one-hour cache on every server (``"ttl": "1h"`` on each cache marker). The
# other keys can change the lifetime too, so each job sets them to ``""``.
CACHE_1H_KEY = "ENABLE_PROMPT_CACHING_1H"
CACHE_KEYS = (
    CACHE_1H_KEY,
    "ENABLE_PROMPT_CACHING_1H_BEDROCK",
    "FORCE_PROMPT_CACHING_5M",
    "DISABLE_PROMPT_CACHING",
)

# The full set. Each environment from ``resolve_inference`` holds exactly these
# keys, and a caller can remove them from ``os.environ`` with the same list.
ENV_KEYS = (*PROVIDER_KEYS, *MODEL_KEYS, NONESSENTIAL_TRAFFIC_KEY, TOOL_SEARCH_KEY, *CACHE_KEYS)

ANTHROPIC_ENDPOINT = "anthropic"


@dataclass(frozen=True)
class ResolvedInference:
    """The model, the CLI environment, and the server label for one job.

    ``endpoint`` is safe to log: it is the host of the server, or
    ``"anthropic"``. It never holds a key or a path. ``tool_search`` is True
    only for a conversation job with tool search on; ``env`` then holds
    ``ENABLE_TOOL_SEARCH``.
    """

    job: str
    model: str
    env: dict[str, str]
    endpoint: str
    tool_search: bool = False


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
    if job == "assignment":
        # The forwarder does not change the model of a call. This model is the
        # one that the check of ``POST /admin/inference/check`` uses.
        return config.core.model
    raise KeyError(f"unknown inference job '{job}'; valid: {INFERENCE_JOBS}")


def job_tool_search(config: JoshuaConfig, job: str) -> bool:
    """Return True when ``job`` uses tool search.

    It is ``inference.<job>.tool_search``, or else the default of the job in
    ``TOOL_SEARCH_DEFAULTS``. A job that is not a conversation job never uses
    tool search. Raises ``KeyError`` for an unknown job.
    """
    block = config.inference.job(job)
    if job not in CONVERSATION_JOBS:
        return False
    if block.tool_search is not None:
        return block.tool_search
    return TOOL_SEARCH_DEFAULTS[job]


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
    job. When the job sets ``base_url``, the server is the job's ``base_url``
    and ``api_key``. When the job sets only ``api_key``, the server is the
    global ``base_url`` with the job's key, and the global key is not used;
    with no global ``base_url``, the job's key is an Anthropic API key. Else
    the job uses the global ``base_url`` and ``api_key``. ``oauth_token`` is
    the subscription token, and it is used only when there is no
    ``base_url`` and no key. Tool search follows ``job_tool_search`` on every
    server. Raises ``KeyError`` for an unknown job.
    """
    inference = config.inference
    block = inference.job(job)
    model = block.model or job_model_setting(config, job)
    if block.base_url is not None:
        base_url: str | None = block.base_url
        api_key = block.api_key or ""
    elif block.api_key is not None:
        base_url = inference.base_url
        api_key = block.api_key
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
        env[CACHE_1H_KEY] = "1"
    elif api_key:
        env["ANTHROPIC_API_KEY"] = api_key
        env[CACHE_1H_KEY] = "1"
    else:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

    tool_search = job_tool_search(config, job)
    if tool_search:
        env[TOOL_SEARCH_KEY] = TOOL_SEARCH_ON

    set_auth = [key for key in AUTH_KEYS if env[key]]
    if len(set_auth) > 1:
        raise AssertionError(f"more than one credential variable is set: {set_auth}")
    return ResolvedInference(
        job=job,
        model=model,
        env=env,
        endpoint=endpoint_label(base_url),
        tool_search=tool_search,
    )
