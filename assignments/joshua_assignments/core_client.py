"""The calls the runner makes to core: the limits, the usage, the item of an
output, and the chat history of a worker.

``limits`` is ``GET <CORE_URL>/v1/limits``. The route has no bearer. It
gives the percent used of the 5-hour window and the weekly window. A failed
read, or an answer that is not a ``LimitsResponse``, gives None: the runner
then treats the limits as unknown.

``post_usage`` is ``POST <CORE_URL>/v1/usage`` with the
``JOSHUA_TOKEN_ASSIGNMENTS`` bearer and one ``UsageWriteRow``. Core keeps
one row for each writer and ``ref``, so a row sent two times is written one
time. A connection error, a timeout, or a 5xx reply is tried again, 3
attempts in all. A 4xx reply is not tried again.

``add_workspace_item`` is ``POST <CORE_URL>/v1/workspaces/<workspace>/items``
with the same bearer and ``person`` for the person the run belongs to. Core
answers 404 when that person is not one of the people of the workspace, so
nothing is recorded then. It is one attempt: an item that does not land is
logged and never fails the tool call that recorded the output. ``assignments``
must be in ``workspaces.allowed_callers``, else core answers 403.

``search_history`` and ``read_history`` are ``POST <CORE_URL>/v1/history/search``
and ``POST <CORE_URL>/v1/history/read`` with the same bearer. The body names
who started the run, ``person`` and the ``workspace`` of a workspace run, and
core alone turns that into the conversations the run may read: the scope is
not decided here. Both are read-only and one attempt; a failed read gives
None, and the tool then tells the model that the history is not readable.

The log names the assignment and the status code, never the token or a body.
A history query and a history message are never logged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from joshua_shared.contracts import (
    HISTORY_READ_PATH,
    HISTORY_SEARCH_PATH,
    LIMITS_PATH,
    USAGE_WRITE_PATH,
    WORKSPACE_ITEMS_PATH,
    HistoryReadRequest,
    HistoryReadResponse,
    HistorySearchRequest,
    HistorySearchResponse,
    LimitsResponse,
    UsageWriteRequest,
    UsageWriteRow,
)
from joshua_shared.log import get_logger
from pydantic import ValidationError

from joshua_assignments.settings import Settings
from joshua_assignments.workspace_items import Item

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

    async def add_workspace_item(
        self, workspace: str, person: str, item: Item, *, assignment_id: int | None = None
    ) -> bool:
        """Record one item in a workspace, for the person the run belongs to.

        Returns True when core accepted it. A person who is not one of the
        people of the workspace gets 404, and nothing is recorded."""
        where = {"assignment_id": assignment_id, "workspace": workspace, "kind": item.kind}
        path = WORKSPACE_ITEMS_PATH.format(workspace=quote(workspace, safe=""))
        headers = {"Authorization": f"Bearer {self.settings.fleet_token}"}
        try:
            async with self._make_client() as client:
                response = await client.post(
                    self._url(path),
                    params={"person": person},
                    json=item.body(),
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            logger.warning(
                {"message": "item not recorded", "error_type": type(exc).__name__, **where}
            )
            return False
        except RuntimeError:
            logger.warning({"message": "item not recorded: no core url", **where})
            return False
        if response.status_code >= 300:
            logger.warning(
                {"message": "core refused the item", "status_code": response.status_code, **where}
            )
            return False
        return True

    async def _history(self, path: str, body: dict[str, Any], where: dict[str, Any]) -> Any:
        """One read-only history call. Gives the raw answer, or None when it failed."""
        headers = {"Authorization": f"Bearer {self.settings.fleet_token}"}
        try:
            async with self._make_client() as client:
                response = await client.post(self._url(path), json=body, headers=headers)
        except (httpx.HTTPError, RuntimeError) as exc:
            logger.warning(
                {"message": "history not read", "error_type": type(exc).__name__, **where}
            )
            return None
        if response.status_code != 200:
            logger.warning(
                {"message": "history not read", "status_code": response.status_code, **where}
            )
            return None
        return response.content

    async def search_history(
        self,
        person: str,
        workspace: str | None,
        query: str,
        limit: int,
        *,
        assignment_id: int | None = None,
    ) -> HistorySearchResponse | None:
        """Search the conversations of the scope of this run. None when the read failed."""
        request = HistorySearchRequest(person=person, workspace=workspace, query=query, limit=limit)
        where = {"assignment_id": assignment_id, "route": "history/search"}
        content = await self._history(HISTORY_SEARCH_PATH, request.model_dump(mode="json"), where)
        if content is None:
            return None
        try:
            return HistorySearchResponse.model_validate_json(content)
        except ValidationError:
            logger.warning({"message": "history not read: the answer is not valid", **where})
            return None

    async def read_history(
        self,
        person: str,
        workspace: str | None,
        start: datetime,
        end: datetime,
        cursor: str | None,
        *,
        assignment_id: int | None = None,
    ) -> HistoryReadResponse | None:
        """Read the conversations of the scope of this run. None when the read failed."""
        request = HistoryReadRequest(
            person=person, workspace=workspace, start=start, end=end, cursor=cursor
        )
        where = {"assignment_id": assignment_id, "route": "history/read"}
        content = await self._history(HISTORY_READ_PATH, request.model_dump(mode="json"), where)
        if content is None:
            return None
        try:
            return HistoryReadResponse.model_validate_json(content)
        except ValidationError:
            logger.warning({"message": "history not read: the answer is not valid", **where})
            return None
