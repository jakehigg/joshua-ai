"""The server, the model, and the credential of each model call in core.

Core reads the subscription token from its environment once, at start, after
the config loads. Then it removes every variable in ``ENV_KEYS`` from
``os.environ``. After that, a credential gets to the CLI only through the
environment that ``resolve`` gives for one job, and that environment holds
only the credential of that job. ``joshua_shared.inference`` does the
resolution. This module holds the token, logs the plan at start, and runs
the check of ``POST /admin/inference/check``.

The token stays in this module and nowhere else. Nothing here logs it.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any
from urllib.parse import urlsplit

from joshua_shared.config import INFERENCE_JOBS, JoshuaConfig
from joshua_shared.inference import (
    AUTH_KEYS,
    ENV_KEYS,
    ResolvedInference,
    resolve_inference,
)
from joshua_shared.log import get_logger, redact

from joshua_core.engine import internet as internet_agent
from joshua_core.engine.usage import Usage
from joshua_core.store.usage_events import record_usage

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
    ``key`` for a job key on the global server, ``model``), or is empty when
    the job uses the global setting. It holds no key.
    """
    out: dict[str, dict[str, Any]] = {}
    for job in INFERENCE_JOBS:
        resolved = resolve(settings, job)
        block = settings.inference.job(job)
        override = []
        if block.base_url is not None:
            override.append("server")
        elif block.api_key is not None:
            override.append("key")
        if block.model is not None:
            override.append("model")
        out[job] = {"endpoint": resolved.endpoint, "model": resolved.model, "override": override}
    return out


def log_plan(settings: JoshuaConfig) -> None:
    """Log the plan of each job in one INFO line, then the search of the internet job.

    The search line says how the internet agent searches: ``mcp:<server>``
    for ``internet.search``, ``anthropic`` for ``WebSearch`` on the Anthropic
    server, or ``none``. A warning follows only for ``none`` while the
    internet agent is on, because it then cannot search.
    """
    jobs = plan(settings)
    logger.info({"message": "inference", "jobs": jobs})
    mode = internet_agent.search_mode(settings.internet, jobs["internet"]["endpoint"])
    search = settings.internet.search
    label = f"mcp:{search.server}" if mode == "mcp" and search is not None else mode
    logger.info(
        {
            "message": "internet search",
            "search": label,
            "enabled": settings.internet.enabled,
        }
    )
    if settings.internet.enabled and mode == "none":
        logger.warning(
            {
                "message": "internet agent has no search: the internet job is not on "
                "anthropic and internet.search is not set",
                "endpoint": jobs["internet"]["endpoint"],
            }
        )


# ── the check of ``POST /admin/inference/check`` ─────────────────────────────

CHECK_SYSTEM = "You answer with one word."
CHECK_PROMPT = "Reply with the word ok."
# The time limit of one check call. It is lower than a turn limit, because the
# check waits for every server and an operator waits for the check.
CHECK_TIMEOUT_S = 30.0
# The maximum length of the ``error`` text of one job.
CHECK_ERROR_CHARS = 200
_REDACTED = "[REDACTED]"

# The usage group of the calls of each job. A voice turn is a chat turn.
_USAGE_KIND = {
    "chat": "chat",
    "voice": "chat",
    "reflection": "reflection",
    "describe": "describe",
    "internet": "internet",
}

Oneshot = Callable[..., Awaitable[str]]


def _secrets(settings: JoshuaConfig, jobs: list[ResolvedInference]) -> list[str]:
    """Every key, token, and URL password that a check call can hold.

    The longest value comes first, so a key that holds a shorter one is
    removed whole.
    """
    values = {_oauth_token, settings.inference.api_key or ""}
    urls = [settings.inference.base_url]
    for job in INFERENCE_JOBS:
        block = settings.inference.job(job)
        values.add(block.api_key or "")
        urls.append(block.base_url)
    for resolved in jobs:
        values.update(resolved.env[key] for key in AUTH_KEYS)
    for url in urls:
        if url:
            parts = urlsplit(url)
            values.update((parts.password or "", parts.username or ""))
    return sorted((v for v in values if v), key=len, reverse=True)


def scrub(text: str, secrets: list[str]) -> str:
    """Return ``text`` with each value of ``secrets`` and each bearer removed,
    on one line and cut to ``CHECK_ERROR_CHARS``."""
    for secret in secrets:
        text = text.replace(secret, _REDACTED)
    text = " ".join(redact(text).split())
    if len(text) > CHECK_ERROR_CHARS:
        text = text[: CHECK_ERROR_CHARS - 3] + "..."
    return text


def _target(resolved: ResolvedInference) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    """The key of one distinct target: the server, the model, and the CLI
    environment, which holds the URL and the credential. It stays in memory."""
    return (resolved.endpoint, resolved.model, tuple(sorted(resolved.env.items())))


async def _check_one(
    resolved: ResolvedInference, oneshot: Oneshot, timeout_s: float
) -> tuple[bool, str | None, str, float, list[Usage]]:
    """Run one check call. Returns ok, the label, the raw error text, the
    seconds, and the usage. The raw text never leaves this module."""
    used: list[Usage] = []
    errors: list[tuple[str, str]] = []
    started = time.monotonic()
    try:
        await oneshot(
            system_prompt=CHECK_SYSTEM,
            user_prompt=CHECK_PROMPT,
            inference=resolved,
            timeout_s=timeout_s,
            usage_out=used,
            error_out=errors,
        )
    except Exception as exc:  # noqa: BLE001 — a failed check is a result, not a 500
        errors.append((type(exc).__name__, str(exc)))
    seconds = round(time.monotonic() - started, 3)
    if errors:
        label, text = errors[0]
        return False, label, text, seconds, used
    return True, None, "", seconds, used


async def check(
    settings: JoshuaConfig,
    *,
    oneshot: Oneshot,
    repo: Any = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Send one short prompt to each distinct target of ``INFERENCE_JOBS``.

    Jobs with the same server, model, and credential share one call. The
    calls run at the same time, each with a limit of ``timeout_s`` (default
    ``CHECK_TIMEOUT_S``, never more than ``inference.timeout_seconds``).
    Returns ``{"ok": bool, "jobs": [...]}`` with ``job``, ``endpoint``,
    ``model``, ``ok``, ``seconds``, and for a failure ``error``: the short
    label and the error text, with every key removed and cut short. The log
    gets the label and the server, never the text. When ``repo`` is given,
    each call writes one usage row in the group of its first job.
    """
    limit = min(
        timeout_s if timeout_s is not None else CHECK_TIMEOUT_S,
        float(settings.inference.timeout_seconds),
    )
    resolved = [resolve(settings, job) for job in INFERENCE_JOBS]
    secrets = _secrets(settings, resolved)
    groups: dict[Any, list[ResolvedInference]] = {}
    for item in resolved:
        groups.setdefault(_target(item), []).append(item)
    firsts = [members[0] for members in groups.values()]
    outcomes = await asyncio.gather(*(_check_one(first, oneshot, limit) for first in firsts))

    by_job: dict[str, dict[str, Any]] = {}
    for first, (ok, label, text, seconds, used) in zip(firsts, outcomes, strict=True):
        members = groups[_target(first)]
        if repo is not None:
            await record_usage(
                repo,
                _USAGE_KIND[first.job],
                used,
                model=first.model,
                endpoint=first.endpoint,
            )
        error = None
        if not ok:
            error = scrub(f"{label}: {text}" if text else str(label), secrets)
            logger.warning(
                {
                    "message": "inference check failed",
                    "error": scrub(str(label), secrets),
                    "jobs": [m.job for m in members],
                    "endpoint": first.endpoint,
                }
            )
        for member in members:
            row: dict[str, Any] = {
                "job": member.job,
                "endpoint": member.endpoint,
                "model": member.model,
                "ok": ok,
                "seconds": seconds,
            }
            if error is not None:
                row["error"] = error
            by_job[member.job] = row
    jobs = [by_job[job] for job in INFERENCE_JOBS]
    failed = sum(not j["ok"] for j in jobs)
    logger.info({"message": "inference check", "calls": len(firsts), "failed": failed})
    return {"ok": all(j["ok"] for j in jobs), "jobs": jobs}


def override_count(settings: JoshuaConfig) -> int:
    """The number of job blocks that set ``base_url`` or ``model``."""
    return sum(
        1
        for job in INFERENCE_JOBS
        if settings.inference.job(job).base_url is not None
        or settings.inference.job(job).model is not None
    )
