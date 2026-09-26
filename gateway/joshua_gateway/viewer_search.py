"""The search by meaning of the viewer: one call to core, and its HTML.

Core owns the index and the embedding model, so the viewer sends the query to
core's ``POST /v1/memory/search`` with its own fleet token and shows the
passages that come back. The search is in the shared scope and names no
person. Core returns only wiki pages, journal entries, and profiles, which any
reader of the wiki can already open.

The viewer needs ``CORE_URL`` and ``JOSHUA_TOKEN_VIEWER``. With either one
missing, :func:`from_env` returns None and the viewer searches by words only.
A call that fails returns None too, and the page says so; the search by words
still shows.

The functions here build data and HTML only. The route lives in ``viewer``.
"""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
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
SNIPPET_CHARS = 280

_WIKI_PREFIX = "wiki/"


class CoreSearch:
    """Search the memory by meaning through core."""

    def __init__(
        self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._transport = transport

    async def search(self, query: str, limit: int = MEANING_MAX_RESULTS) -> SearchResponse | None:
        """Return core's answer, or None when core cannot answer."""
        try:
            body = SearchRequest(query=query, limit=limit).model_dump()
        except ValidationError:
            return None
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
    """The viewer URL of a data-relative path, or None for a path outside the
    wiki, which the viewer does not link."""
    if not path.startswith(_WIKI_PREFIX) or ".." in path.split("/"):
        return None
    return "/wiki/" + quote(path[len(_WIKI_PREFIX) :])


def _snippet(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= SNIPPET_CHARS:
        return flat
    return flat[:SNIPPET_CHARS].rstrip() + "…"


def meaning_html(response: SearchResponse | None) -> str:
    """The "by meaning" section. None means core did not answer."""
    if response is None:
        return (
            "<section class=meaning><h2>By meaning</h2>"
            "<p class=snippet>Search by meaning is not available now.</p></section>"
        )
    items: list[str] = []
    for hit in response.results:
        url = viewer_url(hit.path)
        if url is None:
            continue
        label = hit.title or hit.path
        if hit.heading and hit.heading != hit.title:
            label = f"{label} › {hit.heading}"
        meta = hit.kind if hit.date is None else f"{hit.kind} · {hit.date}"
        items.append(
            f'<li><a href="{escape(url)}">{escape(label)}</a> '
            f"<span class=count>{escape(meta)}</span>"
            f"<br><span class=snippet>{escape(_snippet(hit.text))}</span></li>"
        )
    if not items:
        return "<section class=meaning><h2>By meaning</h2><p>No close match.</p></section>"
    rows = "".join(items)
    return f"<section class=meaning><h2>By meaning</h2><ul class=tree>{rows}</ul></section>"
