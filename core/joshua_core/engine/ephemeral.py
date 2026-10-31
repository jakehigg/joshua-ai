"""The ephemeral worker: one objective, no tools, no history, a fixed answer.

A worker is a small model call that runs beside a turn and returns an object,
not prose. It is the safe way to look at untrusted content, because it can do
nothing with what it reads:

- **Nothing to act with.** A worker reaches no file and no shell. Only the
  internet agent holds tools, and they are web tools: see
  `agent.build_worker_options`. A worker that is told what to do by the
  content it reads can act on none of it.
- **No conversation.** The worker sees one objective and one file. It never
  sees what a person said, in this turn or in any other.
- **A fixed answer.** A JSON schema holds the shape. A value that does not
  match is refused by the model that validates it, so a field that becomes a
  filename or a prompt can hold only what the schema allows.
- **A timeout, and a fallback.** A worker runs on the path of a turn, so it
  gets a short deadline. A failure, a timeout, and an answer that does not
  validate all give `None`, and the caller continues without it.

`run_worker` is the one entry point. The describer in `describe.py` is the
first user of it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from joshua_shared.inference import ResolvedInference
from joshua_shared.log import get_logger
from pydantic import BaseModel, ValidationError

from joshua_core.engine.usage import Usage

logger = get_logger("engine.ephemeral")

# The call the worker makes. The real one is `agent.run_structured`; a test
# passes its own and needs no SDK.
Runner = Callable[..., Awaitable[dict[str, Any] | None]]


def _default_runner() -> Runner:
    from joshua_core.engine.agent import run_structured

    return run_structured


def _default_tool_runner() -> Runner:
    from joshua_core.engine.agent import run_worker_session

    return run_worker_session


async def run_worker[T: BaseModel](
    *,
    name: str,
    system_prompt: str,
    user_text: str,
    answer: type[T],
    inference: ResolvedInference,
    timeout_s: float,
    image: tuple[bytes, str] | None = None,
    document: bytes | None = None,
    runner: Runner | None = None,
    tools: list[str] | None = None,
    max_turns: int = 1,
    hooks: dict[str, Any] | None = None,
    usage_out: list[Usage] | None = None,
    mcp_servers: dict[str, Any] | None = None,
) -> T | None:
    """Run one worker and return its answer, or `None`.

    `answer` is the Pydantic model the reply must match; its JSON schema is what
    the model is asked for. `inference` is the model, the server, and the
    environment of the job, and the runner gets it as it is. `image` is
    `(bytes, media type)` and `document` is a PDF. Give one or neither.

    A worker with no `tools` answers in one turn from what it was given. A
    worker with `tools` may take up to `max_turns` to search and read first, and
    `hooks` gates what a tool may do. `mcp_servers` is empty, or the one
    `search` server of `agent.build_search_server`. `agent.build_worker_options`
    decides which tools and which server a worker may hold at all.

    When `usage_out` is a list, the runner gets it and appends the tokens of
    the call to it, so the caller learns what the call cost. A runner gets the
    argument only when the caller gives it.

    This never raises. Every failure is a warning and a `None`, because the turn
    that called it must continue.
    """
    started = monotonic()
    extra: dict[str, Any] = {} if usage_out is None else {"usage_out": usage_out}
    try:
        if tools:
            if mcp_servers:
                extra["mcp_servers"] = mcp_servers
            call = runner or _default_tool_runner()
            raw = await call(
                system_prompt=system_prompt,
                user_text=user_text,
                schema=answer.model_json_schema(),
                inference=inference,
                timeout_s=timeout_s,
                tools=tools,
                max_turns=max_turns,
                hooks=hooks,
                **extra,
            )
        else:
            call = runner or _default_runner()
            raw = await call(
                system_prompt=system_prompt,
                user_text=user_text,
                schema=answer.model_json_schema(),
                inference=inference,
                timeout_s=timeout_s,
                image=image,
                document=document,
                **extra,
            )
    except Exception as exc:  # noqa: BLE001 — a worker never breaks its caller
        logger.warning({"message": "worker call failed", "worker": name, "error": str(exc)})
        return None

    duration_ms = round((monotonic() - started) * 1000)
    if raw is None:
        logger.info(
            {"message": "worker gave no answer", "worker": name, "duration_ms": duration_ms}
        )
        return None

    try:
        validated = answer.model_validate(raw)
    except ValidationError as exc:
        # The answer did not match the shape. The count alone goes in the log:
        # the content is untrusted, and it is not worth a line.
        logger.warning(
            {
                "message": "worker answer refused",
                "worker": name,
                "errors": len(exc.errors()),
                "duration_ms": duration_ms,
            }
        )
        return None

    logger.info({"message": "worker answered", "worker": name, "duration_ms": duration_ms})
    return validated
