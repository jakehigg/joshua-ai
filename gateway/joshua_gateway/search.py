"""The web search route: ``POST /v1/search``.

The internet agent in core searches the web through this route when its job is
not on the Anthropic server. The body holds one query and nothing else. The
gateway reads the SearXNG instance from ``internet.search`` and calls its JSON
API: ``GET {url}/search?q=<query>&format=json``, with ``categories`` and
``language`` when they are set. So the config is the allowlist: no caller can
choose the host, the path, or another parameter.

The gateway is the outbound boundary, so it holds the optional key of
``internet.search.api_key`` and core does not. The key goes as
``Authorization: Bearer <key>``, and only when it is set.

Only ``core`` may call the route. The answer holds at most ``max_results``
results, each with a ``title``, an ``http`` or ``https`` ``url``, and a
``snippet`` (the ``content`` of the SearXNG result). An error answer is a short
fixed text, never the text of SearXNG. The call log and the log line hold the
length of the query, never the query and never the key.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from joshua_shared import config
from joshua_shared.config import Search
from joshua_shared.contracts import (
    WEB_SEARCH_MAX_SNIPPET_CHARS,
    WEB_SEARCH_MAX_TITLE_CHARS,
    WEB_SEARCH_MAX_URL_CHARS,
    WebSearchRequest,
    WebSearchResponse,
    WebSearchResult,
)
from joshua_shared.fleet_auth import authenticate, load_fleet_tokens
from joshua_shared.log import get_logger
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway.observability import CALL_LOG, CALLERS, now_iso

logger = get_logger("gateway.search")

# Only core searches, for the internet agent.
SEARCH_CALLERS = frozenset({"core"})

# The error of a SearXNG 403. SearXNG refuses `format=json` with a 403 when
# `search.formats` in its settings.yml does not name `json`.
JSON_FORMAT_OFF = (
    "searxng refused the json format (403): add json to search.formats in the "
    "settings.yml of SearXNG, or make sure that the proxy accepts the key"
)


class SearchFailed(Exception):
    """The search did not give results. ``status`` is the HTTP code of the
    route and ``reason`` the short error, with no text of SearXNG."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def find_search() -> Search | None:
    """``internet.search`` from the current config, or None when it is not
    set or the internet agent is off."""
    cfg = config.load()
    if not cfg.internet.enabled:
        return None
    return cfg.internet.search


def search_params(search: Search, query: str) -> dict[str, str]:
    """The query parameters of one SearXNG call."""
    params = {"q": query, "format": "json"}
    if search.categories:
        params["categories"] = search.categories
    if search.language:
        params["language"] = search.language
    return params


async def call_searxng(
    search: Search, query: str, transport: httpx.AsyncBaseTransport | None = None
) -> Any:
    """Call the SearXNG JSON API once and return its decoded answer.

    Raises ``SearchFailed`` for a refusal, an error status, an answer that is
    not JSON, a timeout, or a connection that fails.
    """
    headers = {"Accept": "application/json"}
    if search.api_key:
        headers["Authorization"] = f"Bearer {search.api_key}"
    try:
        async with httpx.AsyncClient(
            timeout=search.timeout_seconds, transport=transport, follow_redirects=False
        ) as client:
            response = await client.get(
                f"{search.url}/search", params=search_params(search, query), headers=headers
            )
    except httpx.TimeoutException as exc:
        raise SearchFailed(504, "timeout") from exc
    except httpx.HTTPError as exc:
        raise SearchFailed(502, f"searxng unreachable: {type(exc).__name__}") from exc
    if response.status_code == 403:
        raise SearchFailed(502, JSON_FORMAT_OFF)
    if response.status_code == 401:
        raise SearchFailed(502, "searxng refused the key (401)")
    if response.status_code != 200:
        raise SearchFailed(502, f"searxng answered {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise SearchFailed(502, "searxng answer is not json") from exc


def _web_url(value: Any) -> str:
    """``value`` when it is an ``http`` or ``https`` URL that fits, else ``""``."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value.lower().startswith(("http://", "https://")):
        return ""
    if len(value) > WEB_SEARCH_MAX_URL_CHARS or any(c.isspace() for c in value):
        return ""
    return value


def _clip(value: Any, limit: int) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def read_results(body: Any, max_results: int) -> list[WebSearchResult]:
    """The ``results`` of a SearXNG answer, at most ``max_results``.

    Each result takes ``url``, ``title``, and ``content`` as the snippet. A
    result with no ``http`` or ``https`` URL is dropped, and one URL is kept
    once.
    """
    items = body.get("results") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise SearchFailed(502, "searxng answer has no results list")
    out: list[WebSearchResult] = []
    seen: set[str] = set()
    for item in items:
        if len(out) >= max_results:
            break
        if not isinstance(item, dict):
            continue
        url = _web_url(item.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(
            WebSearchResult(
                url=url,
                title=_clip(item.get("title"), WEB_SEARCH_MAX_TITLE_CHARS),
                snippet=_clip(item.get("content"), WEB_SEARCH_MAX_SNIPPET_CHARS),
            )
        )
    return out


def _record(
    identity: str,
    status: str,
    error: str | None,
    *,
    query_chars: int = 0,
    results: int = 0,
    duration_ms: float = 0.0,
) -> None:
    """Write one call log entry and one log line. No query, no result, no key."""
    entry = {
        "ts": now_iso(),
        "identity": identity,
        "person": None,
        "conversation": None,
        "server": "searxng",
        "tool": "search",
        "search": True,
        "query_chars": query_chars,
        "results": results,
        "duration_ms": round(duration_ms, 1),
        "status": status,
        "error": error,
    }
    CALL_LOG.record(entry)
    if status == "denied":
        logger.warning({"message": "search refused", **entry})
    elif status == "error":
        CALLERS.record_call(identity)
        logger.warning({"message": "search failed", **entry})
    else:
        CALLERS.record_call(identity)
        logger.info({"message": "search", **entry})


def _authorize(request: Request) -> tuple[JSONResponse | None, str]:
    status, identity = authenticate(
        request.headers.get("authorization"), load_fleet_tokens(), allowed=SEARCH_CALLERS
    )
    if status == 401:
        return JSONResponse({"error": "unauthorized"}, status_code=401), ""
    if status == 403:
        return (
            JSONResponse({"error": f"caller {identity} not allowed on search"}, status_code=403),
            identity or "",
        )
    return None, identity or ""


async def _query(request: Request) -> str | None:
    """The query of the body, with its whitespace made single, or None when
    the body is not valid."""
    try:
        body = WebSearchRequest.model_validate(await request.json())
    except (ValueError, ValidationError):
        return None
    query = " ".join(body.query.split())
    return query or None


async def search_route(request: Request) -> JSONResponse:
    """Run the SearXNG search of the config for one query."""
    denied, identity = _authorize(request)
    if denied is not None:
        return denied
    search = find_search()
    if search is None:
        _record(identity, "denied", "search_not_configured")
        return JSONResponse({"error": "search not configured"}, status_code=404)
    query = await _query(request)
    if query is None:
        _record(identity, "denied", "bad_query")
        return JSONResponse({"error": "bad query"}, status_code=400)

    transport = getattr(request.app.state, "search_transport", None)
    start = time.perf_counter()
    try:
        body = await call_searxng(search, query, transport)
        results = read_results(body, search.max_results)
    except SearchFailed as exc:
        elapsed = (time.perf_counter() - start) * 1000
        _record(identity, "error", exc.reason, query_chars=len(query), duration_ms=elapsed)
        return JSONResponse({"error": exc.reason}, status_code=exc.status)
    elapsed = (time.perf_counter() - start) * 1000
    _record(
        identity,
        "success",
        None,
        query_chars=len(query),
        results=len(results),
        duration_ms=elapsed,
    )
    return JSONResponse(WebSearchResponse(results=results).model_dump())
