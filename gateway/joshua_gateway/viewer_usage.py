"""The usage of the viewer: one call to core.

Core keeps a row for each model call in ``usage_event``. The viewer asks
core's ``GET /v1/usage`` with its own fleet token and answers the tokens of
each group by day: ``chat``, ``reflection``, ``internet``, ``describe``, and
any other group that core names. The React page of ``/usage`` shows them as a
bar chart of the total tokens of each day, stacked by group, a table of the
days by group, a table of the totals and the models of each group, a table of
the workers of each group that has workers, and the counts of each day.

Above the usage, the page shows the subscription limits (``viewer_limits``).

The viewer needs ``CORE_URL`` and ``JOSHUA_TOKEN_VIEWER``. With either one
missing, :func:`from_env` returns None and the page says so. A call that
fails returns None too, and the page says so. The route answers 200 in both
cases.

The call lives here. The route is ``viewer_api.usage``, which answers core's
body as core sent it, so no name of core reaches HTML here. :func:`exact` and
:func:`short` write a count as a person reads it, and the assignments page
uses them too.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
from joshua_shared.contracts import USAGE_DEFAULT_DAYS, USAGE_MAX_DAYS, UsageResponse
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger
from pydantic import ValidationError

logger = get_logger("viewer.usage")

CORE_URL_ENV = "CORE_URL"
VIEWER_TOKEN_ENV = "JOSHUA_TOKEN_VIEWER"
USAGE_PATH = "/v1/usage"

# A person waits for this page, so a slow core must not hold it open.
TIMEOUT_S = 5.0


class CoreUsage:
    """Read the usage by day from core."""

    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._transport = transport

    async def fetch(self, days: int) -> UsageResponse | None:
        """Return core's answer, or None when core cannot answer."""
        try:
            async with FleetClient(
                self._base_url, self._token, TIMEOUT_S, transport=self._transport
            ) as client:
                response = await client.get_json(USAGE_PATH, params={"days": days})
        except httpx.HTTPError as exc:
            logger.warning({"message": "core usage failed", "error": type(exc).__name__})
            return None
        if response.status_code != 200:
            logger.warning({"message": "core usage refused", "status": response.status_code})
            return None
        try:
            return UsageResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            logger.warning({"message": "core usage answered an unknown shape"})
            return None


def from_env(env: Mapping[str, str]) -> CoreUsage | None:
    """A client from ``CORE_URL`` and ``JOSHUA_TOKEN_VIEWER``, or None."""
    base_url = env.get(CORE_URL_ENV)
    token = env.get(VIEWER_TOKEN_ENV)
    if not base_url or not token:
        return None
    return CoreUsage(base_url, token)


def parse_days(raw: str | None) -> int:
    """The ``days`` query value held to ``1..USAGE_MAX_DAYS``. A value that is
    not an integer gives the default."""
    try:
        value = int(raw) if raw else USAGE_DEFAULT_DAYS
    except ValueError:
        value = USAGE_DEFAULT_DAYS
    return max(1, min(value, USAGE_MAX_DAYS))


def exact(value: int) -> str:
    """An exact integer with a thin space between each group of three digits."""
    return f"{value:,}".replace(",", " ")


def short(value: int) -> str:
    """A count for a chart label: ``950``, ``12k``, ``3.4M``."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(value)
