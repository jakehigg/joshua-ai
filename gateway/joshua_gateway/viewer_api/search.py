"""The JSON route of the search: by meaning through core, and by words.

``GET /api/v1/search?q=<query>`` runs both halves of the search and answers
both:

- ``meaning``: core owns the index and the embedding model, so the viewer
  sends the query to core (``viewer_search``). The answer is null when core is
  not configured (``CORE_URL`` or ``JOSHUA_TOKEN_VIEWER`` missing) or did not
  answer, and the page then shows the words half alone.
- ``words``: a substring match over the Markdown the person can read, one hit
  per file, at most ``SEARCH_MAX_RESULTS``. This is not semantic search.

An empty query answers both halves empty and calls core not at all. The route
answers 200 in every case, because the search by words works when core is
down.
"""

from __future__ import annotations

import os
from typing import Any

from joshua_shared.config import Person
from joshua_shared.contracts import SearchResponse
from starlette.requests import Request
from starlette.responses import JSONResponse

from joshua_gateway import viewer_search as search_mod
from joshua_gateway import viewer_wiki as wiki_mod
from joshua_gateway.files_mcp.paths import Root

# The search by words answers at most this many hits.
SEARCH_MAX_RESULTS = 50

# How much of a line or a passage a hit carries.
LINE_CHARS = 200
SNIPPET_CHARS = 280

# Which address of the viewer serves each root, for the link of a hit.
_ROUTE_PREFIX = {"wiki": "/wiki/", "shared": "/shared/"}

# The kind of a hit that is a document, which carries its status, not a date.
_DOCUMENT_KIND = "document"


def _answer(body: dict[str, Any]) -> JSONResponse:
    from joshua_gateway import viewer  # ``viewer`` imports this package

    return JSONResponse(body, headers=viewer._NO_STORE)


def _snippet(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= SNIPPET_CHARS:
        return flat
    return flat[:SNIPPET_CHARS].rstrip() + "…"


def _search(roots: dict[str, Root], query: str) -> list[dict[str, str]]:
    """Substring search over the markdown the person can read. One hit per
    file, newest roots first. This is not semantic search."""
    from joshua_gateway import viewer  # ``viewer`` imports this package

    needle = query.lower()
    hits: list[dict[str, str]] = []
    for name, root in roots.items():
        if name == "attachments":
            continue
        for abs_path, rel in viewer._iter_markdown(root):
            text = viewer._read_text(abs_path)
            if text is None:
                continue
            for line in text.splitlines():
                if needle in line.lower():
                    label = f"{name}/{rel}"
                    if name == "wiki":
                        label = wiki_mod.page_title(text, wiki_mod.stem_title(abs_path.stem))
                    hits.append(
                        {
                            "url": f"{_ROUTE_PREFIX[name]}{rel}",
                            "label": label,
                            "snippet": line.strip()[:LINE_CHARS],
                        }
                    )
                    break
            if len(hits) >= SEARCH_MAX_RESULTS:
                return hits
    return hits


def meaning_json(response: SearchResponse) -> list[dict[str, str]]:
    """The hits of core's answer, in the order core sent them. A path the
    viewer does not link is left out. ``source`` is what the hit is: the kind
    and the date of a page, or ``document`` and the status of a document."""
    hits: list[dict[str, str]] = []
    for hit in response.results:
        url = search_mod.viewer_url(hit.path)
        if url is None:
            continue
        label = hit.title or hit.path
        if hit.kind == _DOCUMENT_KIND:
            source = f"document · {hit.heading}" if hit.heading else "document"
        else:
            if hit.heading and hit.heading != hit.title:
                label = f"{label} › {hit.heading}"
            source = hit.kind if hit.date is None else f"{hit.kind} · {hit.date}"
        hits.append({"url": url, "label": label, "source": source, "snippet": _snippet(hit.text)})
    return hits


async def search(request: Request, person: Person) -> JSONResponse:
    """Both halves of the search of one query.

    The search by meaning names the member who signed in, so core also answers
    with their documents and the shared ones; a guest names nobody. An empty
    query answers both halves empty.
    """
    from joshua_gateway import viewer  # ``viewer`` imports this package

    query = (request.query_params.get("q") or "").strip()
    core = search_mod.from_env(os.environ)
    if not query:
        return _answer(
            {
                "query": "",
                "meaning_configured": core is not None,
                "meaning": [],
                "words": [],
            }
        )
    meaning = None
    if core is not None:
        reader = person.id if person.role == "member" else None
        response = await core.search(query, person=reader)
        if response is not None:
            meaning = meaning_json(response)
    return _answer(
        {
            "query": query,
            "meaning_configured": core is not None,
            "meaning": meaning,
            "words": _search(viewer._roots_for(person), query),
        }
    )
