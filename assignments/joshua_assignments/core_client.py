"""The two calls the runner makes to core: the limits and the usage.

``limits`` is ``GET <CORE_URL>/v1/limits``. The route has no bearer. It
gives the percent used of the 5-hour window and the weekly window. A failed
read, or an answer that is not a ``LimitsResponse``, gives None: the runner
then treats the limits as unknown.

``post_usage`` is ``POST <CORE_URL>/v1/usage`` with the
``JOSHUA_TOKEN_ASSIGNMENTS`` bearer and one ``UsageWriteRow``. Core keeps
one row for each writer and ``ref``, so a row sent two times is written one
time. A connection error, a timeout, or a 5xx reply is tried again, 3
attempts in all. A 4xx reply is not tried again.

The log names the assignment and the status code, never the token or a body.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
from joshua_shared.contracts import (
    LIMITS_PATH,
    USAGE_WRITE_PATH,
    LimitsResponse,
    UsageWriteRequest,
    UsageWriteRow,
)
from joshua_shared.log import get_logger
from pydantic import ValidationError

from joshua_assignments.settings import Settings

logger = get_logger("joshua_assignments.core_client")

REQUEST_TIMEOUT_S = 15.0
ATTEMPTS = 3
RETRY_BACKOFF_S = 2.0


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)


class CoreClient:
    """Calls to core with the container's fleet token."""

    def __init__(
        self,
        settings: Settings,
        *,
        make_client: Callable[[], httpx.AsyncClient] = _make_client,
        backoff_s: float = RETRY_BACKOFF_S,
    ) -> None:
        self.settings = settings
        self._make_client = make_client
        self._backoff_s = backoff_s

    def _url(self, path: str) -> str:
        if not self.settings.core_url:
            raise RuntimeError("CORE_URL is not set")
        return self.settings.core_url.rstrip("/") + path

    async def limits(self) -> LimitsResponse | None:
        """Core's answer of ``GET /v1/limits``, or None when it cannot be read."""
        try:
            async with self._make_client() as client:
                response = await client.get(self._url(LIMITS_PATH))
        except httpx.HTTPError as exc:
            logger.warning({"message": "limits not read", "error_type": type(exc).__name__})
            return None
        if response.status_code != 200:
            logger.warning({"message": "limits not read", "status_code": response.status_code})
            return None
        try:
            return LimitsResponse.model_validate_json(response.content)
        except ValidationError:
            logger.warning({"message": "limits not read: the answer is not valid"})
            return None

    async def post_usage(self, row: UsageWriteRow) -> bool:
        """Send one usage row to core. Returns True when core accepted it."""
        where = {"assignment_id": row.run_id, "ref": row.ref}
        body = UsageWriteRequest(rows=[row]).model_dump(mode="json")
        headers = {"Authorization": f"Bearer {self.settings.fleet_token}"}
        async with self._make_client() as client:
            for attempt in range(1, ATTEMPTS + 1):
                try:
                    response = await client.post(
                        self._url(USAGE_WRITE_PATH), json=body, headers=headers
                    )
                except httpx.HTTPError as exc:
                    logger.warning(
                        {
                            "message": "usage attempt failed",
                            "attempt": attempt,
                            "error_type": type(exc).__name__,
                            **where,
                        }
                    )
                else:
                    if response.status_code == 200:
                        return True
                    logger.warning(
                        {
                            "message": "core refused the usage",
                            "status_code": response.status_code,
                            **where,
                        }
                    )
                    if response.status_code < 500:
                        return False
                if attempt < ATTEMPTS:
                    await asyncio.sleep(self._backoff_s * attempt)
        logger.error({"message": "usage not sent after retries", **where})
        return False
