"""The server, the model, and the credential of each model call in core.

Core reads the subscription token from its environment once, at start, after
the config loads. Then it removes every variable in ``ENV_KEYS`` from
``os.environ``. After that, a credential gets to the CLI only through the
environment that ``resolve`` gives for one job, and that environment holds
only the credential of that job. ``joshua_shared.inference`` does the
resolution. This module holds the token and logs the plan at start.

The token stays in this module and nowhere else. Nothing here logs it.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from typing import Any

from joshua_shared.config import INFERENCE_JOBS, JoshuaConfig
from joshua_shared.inference import (
    ANTHROPIC_ENDPOINT,
    ENV_KEYS,
    ResolvedInference,
    resolve_inference,
)
from joshua_shared.log import get_logger

logger = get_logger("engine.inference")

OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

# The job of a turn when its profile names none.
DEFAULT_TURN_JOB = "chat"

_oauth_token = ""


def take_credentials(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """Keep the subscription token, then remove every key in ``ENV_KEYS``.

    ``environ`` is ``os.environ`` when it is None. Call this after the config
    loads, because the config can refer to a variable that this removes.
    Returns the names that were removed, and never a value.
    """
    global _oauth_token
    env = os.environ if environ is None else environ
    _oauth_token = env.get(OAUTH_TOKEN_ENV, "")
    removed = [key for key in ENV_KEYS if key in env]
    for key in removed:
        env.pop(key, None)
    return removed


def resolve(settings: JoshuaConfig, job: str) -> ResolvedInference:
    """Return the model, the CLI environment, and the server label for ``job``.

    This is cheap and does no I/O, so each call resolves again.
    """
    return resolve_inference(settings, job, oauth_token=_oauth_token)


def turn_job(profile: Any) -> str:
    """The job of a turn: ``voice`` for a spoken profile, else ``chat``."""
    job = getattr(profile, "job", None)
    return job if isinstance(job, str) and job else DEFAULT_TURN_JOB


def plan(settings: JoshuaConfig) -> dict[str, dict[str, Any]]:
    """For each job: the server label, the model, and what its block changes.

    ``override`` names the fields that the job block sets (``server``,
    ``model``), or is empty when the job uses the global setting. It holds no
    key.
    """
    out: dict[str, dict[str, Any]] = {}
    for job in INFERENCE_JOBS:
        resolved = resolve(settings, job)
        block = settings.inference.job(job)
        override = []
        if block.base_url is not None:
            override.append("server")
        if block.model is not None:
            override.append("model")
        out[job] = {"endpoint": resolved.endpoint, "model": resolved.model, "override": override}
    return out


def log_plan(settings: JoshuaConfig) -> None:
    """Log the plan of each job in one INFO line, and warn about web search.

    The internet agent searches with ``WebSearch``, which runs on Anthropic's
    side. On another server the search fails, so this warns at start.
    """
    jobs = plan(settings)
    logger.info({"message": "inference", "jobs": jobs})
    internet = jobs["internet"]["endpoint"]
    if settings.internet.enabled and internet != ANTHROPIC_ENDPOINT:
        logger.warning(
            {
                "message": "internet job is not on anthropic; WebSearch runs on "
                "Anthropic's side and will fail on this server",
                "endpoint": internet,
            }
        )
