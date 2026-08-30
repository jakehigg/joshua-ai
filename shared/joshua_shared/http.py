"""Outbound HTTP client that carries this container's fleet identity token.

``FleetClient`` wraps ``httpx.AsyncClient`` and adds ``Authorization: Bearer
<token>`` to every request. ``post_json`` and ``get_json`` retry on connection
errors only — never on a 4xx or 5xx response — with exponential backoff, up to
three attempts. Each request logs its ``status`` and ``duration_ms``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx

logger = logging.getLogger("http")

MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 0.2

# Connection errors are the only retryable failures. A 4xx/5xx response does not
# raise, so it is returned to the caller and never retried.
_RETRY_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)


class FleetClient:
    """Async HTTP client that sends this container's fleet token on every call."""

    def __init__(
        self,
        base_url: str,
        identity_token: str,
        timeout: float = 15.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {identity_token}"},
            transport=transport,
        )

    async def __aenter__(self) -> FleetClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying client."""
        await self._client.aclose()

    async def post_json(self, url: str, json: Any = None, **kwargs: Any) -> httpx.Response:
        """POST a JSON body and return the response."""
        return await self._request("POST", url, json=json, **kwargs)

    async def get_json(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET a URL and return the response."""
        return await self._request("GET", url, **kwargs)

    def stream(
        self, method: str, url: str, **kwargs: Any
    ) -> AbstractAsyncContextManager[httpx.Response]:
        """Open a streaming request as an async context manager.

        The bearer header still rides on the request. Connection retries do not
        apply to a stream; the caller consumes the body over the open response.
        """
        return self._client.stream(method, url, **kwargs)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_exc: BaseException | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            start = time.monotonic()
            try:
                response = await self._client.request(method, url, **kwargs)
            except _RETRY_ERRORS as exc:
                last_exc = exc
                logger.warning(
                    {
                        "message": "fleet request connection error",
                        "method": method,
                        "url": url,
                        "attempt": attempt,
                        "duration_ms": round((time.monotonic() - start) * 1000, 1),
                        "error": type(exc).__name__,
                    }
                )
                if attempt < MAX_ATTEMPTS:
                    await asyncio.sleep(BACKOFF_BASE_S * 2 ** (attempt - 1))
                    continue
                raise
            logger.info(
                {
                    "message": "fleet request",
                    "method": method,
                    "url": url,
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - start) * 1000, 1),
                }
            )
            return response
        raise last_exc  # pragma: no cover
