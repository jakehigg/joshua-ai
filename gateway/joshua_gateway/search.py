"""The web search route: ``POST /v1/search``.

The internet agent in core searches the web through this route. The body holds
one query and nothing else. The gateway reads the server, the tool, and the
fixed arguments from ``internet.search``, so the config is the allowlist, and
no caller can ask for another tool or other arguments. The search provider
key stays in the configuration of the upstream, in the gateway.

Only ``core`` may call the route. No person calls it, so the ``allow`` list of
the server does not apply: ``allow:`` with no value keeps the tool away from
every person and still lets the internet agent search. The gateway refuses,
and logs, a call when:

- ``internet.search`` is not set, or the internet agent is off (404);
- the tool filter of the server hides the tool, or the upstream does not
  declare it (403);
- the ``class`` map of the server marks the tool ``write-local`` or ``act``
  (403);
- the upstream declares the tool with ``destructiveHint: true`` and not
  ``readOnlyHint: true`` (403).

Otherwise the gateway calls the tool once, with a timeout, and reads the
results from the answer: a JSON list, a JSON object that holds a list, or, as
a last step, the ``http`` and ``https`` URLs in plain text. The call log and
the log line hold the length of the query, never the query.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

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

from joshua_gateway.catalog import ServerSpec
from joshua_gateway.observability import (
    CALL_LOG,
    CALLERS,
    conversation_ctx,
    identity_ctx,
    now_iso,
    person_ctx,
    role_ctx,
)
from joshua_gateway.snapshots import configured_class, is_destructive
from joshua_gateway.upstream import Upstream

logger = get_logger("gateway.search")

# Only core searches, for the internet agent.
SEARCH_CALLERS = frozenset({"core"})

# The tool classes that change something. A search must only read.
_REFUSED_CLASSES = ("write-local", "act")

# The keys that can hold the result list in a JSON answer, in order.
_LIST_KEYS = ("results", "items", "organic", "organic_results", "data", "web", "news")
_URL_KEYS = ("url", "link", "href")
_TITLE_KEYS = ("title", "name")
_SNIPPET_KEYS = ("snippet", "content", "description", "body", "text", "summary")

_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
_TITLE_PREFIX_RE = re.compile(r"^\s*(?:title|name)\s*:\s*", re.IGNORECASE)
_URL_LINE_RE = re.compile(r"^\s*(?:url|link|href)\s*:\s*\S+\s*$", re.IGNORECASE)
_LABEL_RE = re.compile(r"^\s*(?:description|snippet|content)\s*:\s*", re.IGNORECASE)


class SearchRefused(Exception):
    """The gateway refuses this search. ``status`` is the HTTP code."""

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


def check_policy(spec: ServerSpec, search: Search) -> None:
    """Raise ``SearchRefused`` when the config alone refuses the search tool."""
    if not spec.tool_filter.permits(search.tool):
        raise SearchRefused(403, "tool_not_exposed")
    klass = configured_class(spec, search.tool)
    if klass in _REFUSED_CLASSES:
        raise SearchRefused(403, f"tool_class_{klass}")


def coerce_args(args: dict[str, Any], schema: Any) -> dict[str, Any]:
    """``args`` with each text value changed to the type the tool declares.

    YAML gives every value as text. A tool that declares ``count`` an integer
    refuses ``"5"``, so a value of a property of type ``integer``, ``number``,
    or ``boolean`` is converted. A value that does not convert stays as it is,
    and the tool decides.
    """
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    out: dict[str, Any] = {}
    for key, value in args.items():
        declared = properties.get(key, {}) if isinstance(properties, dict) else {}
        kind = declared.get("type") if isinstance(declared, dict) else None
        out[key] = _convert(value, kind) if isinstance(value, str) else value
    return out


def _convert(value: str, kind: Any) -> Any:
    try:
        if kind == "integer":
            return int(value)
        if kind == "number":
            return float(value)
    except ValueError:
        return value
    if kind == "boolean" and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


async def call_search(up: Upstream, search: Search, query: str) -> Any:
    """List the tools, check the declared tool, then call it once.

    Raises ``SearchRefused`` when the upstream does not declare the tool or
    declares it destructive.
    """
    listed = await up.forward(lambda session: session.list_tools(), repeatable=True)
    declared = next((t for t in listed.tools if t.name == search.tool), None)
    if declared is None:
        raise SearchRefused(403, "tool_not_exposed")
    if is_destructive(declared):
        raise SearchRefused(403, "tool_destructive")
    arguments = coerce_args(dict(search.args), getattr(declared, "input_schema", None))
    arguments[search.query_arg] = query
    return await up.forward(
        lambda session: session.call_tool(search.tool, arguments), repeatable=False
    )


# ── reading the results ─────────────────────────────────────────────────────


def normalize(result: Any, max_results: int) -> list[WebSearchResult]:
    """The results of one tool answer, at most ``max_results``.

    The structured content of the answer comes first. Then each text part as
    JSON, then each line of the text as JSON, and then, when no part is JSON,
    the text as plain text. Only an ``http`` or ``https`` URL is kept, and one URL is kept once.
    """
    structured = getattr(result, "structured_content", None)
    if structured:
        found = _from_items(_find_list(structured), max_results)
        if found:
            return found
    parts = [
        getattr(part, "text", "")
        for part in getattr(result, "content", None) or []
        if getattr(part, "type", None) == "text"
    ]
    text = "\n".join(parts)
    was_json = False
    for pieces in (parts, text.splitlines()):
        items: list[Any] = []
        for piece in pieces:
            payload = _json(piece)
            was_json = was_json or payload is not None
            items.extend(_find_list(payload))
        found = _from_items(items, max_results)
        if found:
            return found
    # A JSON answer with no result has no result. Its text is not searched
    # for URLs, because those are not the URLs of results.
    return [] if was_json else _from_text(text, max_results)


def _json(text: str) -> Any:
    """``text`` read as JSON, or None when it is not a JSON list or object."""
    text = text.strip()
    if text[:1] not in ("[", "{"):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _find_list(payload: Any, depth: int = 0) -> list[Any]:
    """The list of result objects in a JSON answer, or an empty list."""
    if depth > 4:
        return []
    if isinstance(payload, list):
        if any(isinstance(item, dict) and _first(item, _URL_KEYS) for item in payload):
            return payload
        for item in payload:
            found = _find_list(item, depth + 1)
            if found:
                return found
        return []
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            if key in payload:
                found = _find_list(payload[key], depth + 1)
                if found:
                    return found
        if _first(payload, _URL_KEYS):
            return [payload]
    return []


def _first(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _web_url(value: str) -> str:
    """``value`` when it is an ``http`` or ``https`` URL that fits, else ``""``."""
    value = value.strip()
    if not value.lower().startswith(("http://", "https://")):
        return ""
    if len(value) > WEB_SEARCH_MAX_URL_CHARS or any(c.isspace() for c in value):
        return ""
    return value


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _from_items(items: list[Any], max_results: int) -> list[WebSearchResult]:
    out: list[WebSearchResult] = []
    seen: set[str] = set()
    for item in items:
        if len(out) >= max_results:
            break
        if not isinstance(item, dict):
            continue
        url = _web_url(_first(item, _URL_KEYS))
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(
            WebSearchResult(
                url=url,
                title=_clip(_first(item, _TITLE_KEYS), WEB_SEARCH_MAX_TITLE_CHARS),
                snippet=_clip(_first(item, _SNIPPET_KEYS), WEB_SEARCH_MAX_SNIPPET_CHARS),
            )
        )
    return out


def _from_text(text: str, max_results: int) -> list[WebSearchResult]:
    """The results of a plain text answer.

    A block is a group of lines with no empty line between them, or one line
    when the text has no empty line. The first URL of a block is the result.
    The ``Title:`` line, or else the first line with no URL, is the title, and
    the other lines are the snippet.
    """
    if not text.strip():
        return []
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) == 1:
        blocks = [line for line in text.splitlines() if line.strip()]
    out: list[WebSearchResult] = []
    seen: set[str] = set()
    for block in blocks:
        if len(out) >= max_results:
            break
        match = _URL_RE.search(block)
        url = _web_url(match.group(0).rstrip(".,;:")) if match else ""
        if not url or url in seen:
            continue
        seen.add(url)
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        title = ""
        for line in lines:
            if _TITLE_PREFIX_RE.match(line):
                title = _TITLE_PREFIX_RE.sub("", line)
                break
        if not title and len(lines) > 1:
            title = next((line for line in lines if not _URL_RE.search(line)), "")
        rest = [
            _LABEL_RE.sub("", line)
            for line in lines
            if line != title and not _TITLE_PREFIX_RE.match(line) and not _URL_LINE_RE.match(line)
        ]
        out.append(
            WebSearchResult(
                url=url,
                title=_clip(title, WEB_SEARCH_MAX_TITLE_CHARS),
                snippet=_clip(" ".join(rest), WEB_SEARCH_MAX_SNIPPET_CHARS),
            )
        )
    return out


# ── the route ───────────────────────────────────────────────────────────────


def _record(
    identity: str,
    search: Search | None,
    status: str,
    error: str | None,
    *,
    query_chars: int = 0,
    results: int = 0,
    duration_ms: float = 0.0,
) -> None:
    """Write one call log entry and one log line. No query and no result."""
    entry = {
        "ts": now_iso(),
        "identity": identity,
        "person": None,
        "conversation": None,
        "server": search.server if search else None,
        "tool": search.tool if search else None,
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
    else:
        CALLERS.record_call(identity)
        logger.info({"message": "tool call", **entry})


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
    """The query of the body, stripped, or None when the body is not valid."""
    try:
        body = WebSearchRequest.model_validate(await request.json())
    except (ValueError, ValidationError):
        return None
    query = " ".join(body.query.split())
    return query or None


async def search_route(request: Request) -> JSONResponse:
    """Run the configured search for one query and answer ``WebSearchResponse``."""
    denied, identity = _authorize(request)
    if denied is not None:
        return denied
    search = find_search()
    if search is None:
        _record(identity, None, "denied", "search_not_configured")
        return JSONResponse({"error": "search not configured"}, status_code=404)
    query = await _query(request)
    if query is None:
        _record(identity, search, "denied", "bad_query")
        return JSONResponse({"error": "bad query"}, status_code=400)
    chars = len(query)
    specs: dict[str, ServerSpec] = getattr(request.app.state, "specs", {})
    spec = specs.get(search.server)
    if spec is None or spec.is_builtin or spec.has_identities:
        _record(identity, search, "denied", "unknown_server", query_chars=chars)
        return JSONResponse({"error": "unknown server"}, status_code=404)
    try:
        check_policy(spec, search)
    except SearchRefused as exc:
        _record(identity, search, "denied", exc.reason, query_chars=chars)
        return JSONResponse({"error": exc.reason}, status_code=exc.status)

    upstreams: dict[str, Upstream] = getattr(request.app.state, "upstreams", {})
    up = upstreams.get(spec.name)
    if up is None or not up.serving:
        _record(identity, search, "error", "upstream_unavailable", query_chars=chars)
        return JSONResponse({"error": "search server unavailable"}, status_code=503)

    tokens = (
        identity_ctx.set(identity),
        person_ctx.set(None),
        conversation_ctx.set(None),
        role_ctx.set(None),
    )
    start = time.perf_counter()
    try:
        result = await asyncio.wait_for(call_search(up, search, query), search.timeout_seconds)
    except SearchRefused as exc:
        _record(identity, search, "denied", exc.reason, query_chars=chars)
        return JSONResponse({"error": exc.reason}, status_code=exc.status)
    except TimeoutError:
        elapsed = (time.perf_counter() - start) * 1000
        _record(identity, search, "error", "timeout", query_chars=chars, duration_ms=elapsed)
        return JSONResponse({"error": "timeout"}, status_code=504)
    except Exception as exc:  # noqa: BLE001 — any upstream fault is one 502
        elapsed = (time.perf_counter() - start) * 1000
        _record(
            identity, search, "error", type(exc).__name__, query_chars=chars, duration_ms=elapsed
        )
        return JSONResponse({"error": "search server failed the call"}, status_code=502)
    finally:
        identity_ctx.reset(tokens[0])
        person_ctx.reset(tokens[1])
        conversation_ctx.reset(tokens[2])
        role_ctx.reset(tokens[3])

    elapsed = (time.perf_counter() - start) * 1000
    if bool(getattr(result, "is_error", False)):
        _record(identity, search, "error", "tool error", query_chars=chars, duration_ms=elapsed)
        return JSONResponse({"error": "search tool error"}, status_code=502)
    results = normalize(result, search.max_results)
    _record(
        identity,
        search,
        "success",
        None,
        query_chars=chars,
        results=len(results),
        duration_ms=elapsed,
    )
    return JSONResponse(WebSearchResponse(results=results).model_dump())
