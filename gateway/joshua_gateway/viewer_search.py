"""The search by meaning of the viewer: one call to core.

Core owns the index and the embedding model, so the viewer sends the query to
core's ``POST /v1/memory/search`` with its own fleet token and answers with the
passages that come back. The search is in the shared scope and names no
person. Core returns only wiki pages, journal entries, and profiles, which any
reader of the wiki can already open. For a member, a second search names the
person and the ``documents`` source: core returns the documents of that person
and the shared ones, by title and summary, and each one links to
``/documents/<owner>/<slug>``.

The viewer needs ``CORE_URL`` and ``JOSHUA_TOKEN_VIEWER``. With either one
missing, :func:`from_env` returns None and the viewer searches by words only.
A call that fails returns None too, and the page shows the search by words
alone.

The route is ``viewer_api.search``, which shapes the answer of this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from urllib.parse import quote

import httpx
from joshua_shared.contracts import SearchRequest, SearchResponse
from joshua_shared.http import FleetClient
from joshua_shared.log import get_logger
from pydantic import ValidationError

logger = get_logger("viewer.search")

CORE_URL_ENV = "CORE_URL"
VIEWER_TOKEN_ENV = "JOSHUA_TOKEN_VIEWER"
SEARCH_PATH = "/v1/memory/search"

# A person waits for this page, so a slow core must not hold it open.
TIMEOUT_S = 5.0
MEANING_MAX_RESULTS = 10

_WIKI_PREFIX = "wiki/"
_DOCUMENTS_PREFIX = "documents/"


class CoreSearch:
    """Search the memory by meaning through core."""

    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._transport = transport

    async def search(
        self, query: str, limit: int = MEANING_MAX_RESULTS, *, person: str | None = None
    ) -> SearchResponse | None:
        """Return core's answer, or None when core cannot answer.

        With ``person``, the answer also holds the documents of that person and
        the shared ones, in order of score. A failed search of documents leaves
        them out."""
        try:
            body = SearchRequest(query=query, limit=limit).model_dump()
            documents = (
                SearchRequest(
                    query=query, limit=limit, source="documents", person=person
                ).model_dump()
                if person is not None
                else None
            )
        except ValidationError:
            return None
        if documents is None:
            return await self._post(body)
        files, docs = await asyncio.gather(self._post(body), self._post(documents))
        if files is None:
            return None
        if docs is None:
            return files
        merged = sorted([*files.results, *docs.results], key=lambda hit: -hit.score)
        return SearchResponse(results=merged[:limit])

    async def _post(self, body: dict[str, object]) -> SearchResponse | None:
        try:
            async with FleetClient(
                self._base_url, self._token, TIMEOUT_S, transport=self._transport
            ) as client:
                response = await client.post_json(SEARCH_PATH, json=body)
        except httpx.HTTPError as exc:
            logger.warning({"message": "core search failed", "error": type(exc).__name__})
            return None
        if response.status_code != 200:
            logger.warning({"message": "core search refused", "status": response.status_code})
            return None
        try:
            return SearchResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            logger.warning({"message": "core search answered an unknown shape"})
            return None


def from_env(env: Mapping[str, str]) -> CoreSearch | None:
    """A client from ``CORE_URL`` and ``JOSHUA_TOKEN_VIEWER``, or None."""
    base_url = env.get(CORE_URL_ENV)
    token = env.get(VIEWER_TOKEN_ENV)
    if not base_url or not token:
        return None
    return CoreSearch(base_url, token)


def viewer_url(path: str) -> str | None:
    """The viewer URL of a data-relative path, or None for a path the viewer
    does not link. A wiki page is ``/wiki/<path>``; a document
    ``documents/<owner>/<slug>.md`` is ``/documents/<owner>/<slug>``."""
    parts = path.split("/")
    if ".." in parts:
        return None
    if path.startswith(_DOCUMENTS_PREFIX):
        if len(parts) < 3 or not path.endswith(".md") or "" in parts:
            return None
        return "/documents/" + quote(path[len(_DOCUMENTS_PREFIX) : -len(".md")])
    if not path.startswith(_WIKI_PREFIX):
        return None
    return "/wiki/" + quote(path[len(_WIKI_PREFIX) :])
