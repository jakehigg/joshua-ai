"""Lifecycle helpers for FastAPI containers.

``lifespan_with`` builds an ASGI lifespan context manager. It runs the startup
hooks on entry and the shutdown hooks on exit. Each hook can be sync or async.
The shutdown hooks share one ``graceful_shutdown_timeout``; if they do not
finish in time the wait stops so the process can exit.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger("lifecycle")

Hook = Callable[[], Any]

DEFAULT_SHUTDOWN_TIMEOUT_S = 10.0


async def _run(hook: Hook) -> None:
    result = hook()
    if inspect.isawaitable(result):
        await result


def lifespan_with(
    startup: list[Hook] | None = None,
    shutdown: list[Hook] | None = None,
    *,
    graceful_shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_S,
) -> Callable[[Any], Any]:
    """Build a FastAPI ``lifespan`` from startup and shutdown hooks."""
    startup_hooks = list(startup or [])
    shutdown_hooks = list(shutdown or [])

    async def run_shutdown() -> None:
        for hook in shutdown_hooks:
            try:
                await _run(hook)
            except Exception:  # noqa: BLE001 — one failed hook must not block the rest
                logger.exception("shutdown hook failed")

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        for hook in startup_hooks:
            await _run(hook)
        try:
            yield
        finally:
            if shutdown_hooks:
                try:
                    await asyncio.wait_for(run_shutdown(), timeout=graceful_shutdown_timeout)
                except TimeoutError:
                    logger.warning(
                        "shutdown hooks exceeded %.1fs timeout", graceful_shutdown_timeout
                    )

    return lifespan
