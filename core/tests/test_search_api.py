"""`POST /v1/memory/search`: who may call it, and what it can never return.

A FakeStore stands in for pgvector and records what the route asked for.
Embedding is a constant vector, so no model loads.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from joshua_core.memory import embed as embed_module
from joshua_core.memory.models import KbChunk
from joshua_core.search_api import SEARCH_PATH, build_search_router
from joshua_shared.config import Memory, MemorySearch
from starlette.testclient import TestClient

VIEWER = "viewer-token"
MCP = "mcp-token"
CHANNELS = "channels-token"


def _chunk(kind: str, path: str, *, source: str = "files", person_id: str | None = None):
    return KbChunk(
        person_id=person_id,
        source=source,
        kind=kind,
        path=path,
        title=path.rsplit("/", 1)[-1],
        heading="",
        text=f"text of {path}",
        similarity=0.9,
    )


class FakeStore:
    """Returns every row it holds, whatever filter the route asks for, so the
    test proves the route filters again and does not trust the database."""

    def __init__(self, rows: list[KbChunk]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def kb_search(
        self, person_id, embedding, *, k, min_sim, sources=None, kinds=None, exclude_kinds=None
    ):
        self.calls.append({"person_id": person_id, "sources": sources, "kinds": kinds, "k": k})
        return list(self.rows)


_ROWS = [
    _chunk("wiki", "wiki/garden.md"),
    _chunk("journal", "wiki/journal/2026/09/26/garden.md"),
    _chunk("profile", "wiki/people/alex.md"),
    _chunk("people", "people/alex/attachments/2026/09/scan.pdf"),
    _chunk("skill", "wiki/skills/movie.md"),
    _chunk("shared", "shared/old.md"),
    _chunk("wiki", "wiki/memo.md", source="memos", person_id="alex"),
]


def _client(monkeypatch, *, allowed=("viewer", "mcp"), rows=_ROWS, embed=True):
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)
    monkeypatch.setenv("JOSHUA_TOKEN_MCP", MCP)
    monkeypatch.setenv("JOSHUA_TOKEN_CHANNELS", CHANNELS)
    monkeypatch.setattr(
        embed_module, "embed", lambda text, model="": [0.1] * 384 if embed else None
    )
    store = FakeStore(rows)
    settings = SimpleNamespace(memory=Memory(search=MemorySearch(allowed_callers=list(allowed))))
    app = FastAPI()
    app.state.ctx = SimpleNamespace(settings=settings, memory=store)
    app.include_router(build_search_router())
    client = TestClient(app)
    client.store = store  # type: ignore[attr-defined]
    return client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_no_bearer_is_401(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert client.post(SEARCH_PATH, json={"query": "garden"}).status_code == 401


def test_no_bearer_is_401_even_with_a_bad_body(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert client.post(SEARCH_PATH, json={"kinds": ["people"]}).status_code == 401


def test_body_that_is_not_json_is_422(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.post(SEARCH_PATH, content=b"not json", headers=_bearer(VIEWER))
    assert response.status_code == 422


def test_wrong_bearer_is_401(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.post(SEARCH_PATH, json={"query": "garden"}, headers=_bearer("nope"))
    assert response.status_code == 401


def test_identity_off_the_list_is_403(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.post(SEARCH_PATH, json={"query": "garden"}, headers=_bearer(CHANNELS))
    assert response.status_code == 403
    assert client.store.calls == []


def test_empty_allowlist_turns_the_route_off(monkeypatch) -> None:
    client = _client(monkeypatch, allowed=())
    for token in (VIEWER, MCP, CHANNELS):
        response = client.post(SEARCH_PATH, json={"query": "garden"}, headers=_bearer(token))
        assert response.status_code == 403


def test_returns_only_wiki_journal_and_profile_rows(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.post(SEARCH_PATH, json={"query": "garden"}, headers=_bearer(VIEWER))
    assert response.status_code == 200
    paths = [hit["path"] for hit in response.json()["results"]]
    assert paths == [
        "wiki/garden.md",
        "wiki/journal/2026/09/26/garden.md",
        "wiki/people/alex.md",
    ]


def test_never_returns_a_persons_attachment_or_a_skill(monkeypatch) -> None:
    client = _client(monkeypatch)
    body = client.post(SEARCH_PATH, json={"query": "scan"}, headers=_bearer(MCP)).json()
    paths = {hit["path"] for hit in body["results"]}
    assert "people/alex/attachments/2026/09/scan.pdf" not in paths
    assert "wiki/skills/movie.md" not in paths
    assert "wiki/memo.md" not in paths  # a person-scoped row from another source
    assert "shared/old.md" not in paths


def test_searches_the_shared_scope_of_the_files_source(monkeypatch) -> None:
    client = _client(monkeypatch)
    client.post(SEARCH_PATH, json={"query": "garden", "limit": 4}, headers=_bearer(VIEWER))
    call = client.store.calls[0]
    assert call["person_id"] is None
    assert call["sources"] == ("files",)
    assert call["kinds"] == ("wiki", "journal", "profile")


def test_kinds_narrow_the_search(monkeypatch) -> None:
    client = _client(monkeypatch)
    body = client.post(
        SEARCH_PATH, json={"query": "garden", "kinds": ["journal"]}, headers=_bearer(VIEWER)
    ).json()
    assert client.store.calls[0]["kinds"] == ("journal",)
    # The store here ignores the filter, so this proves the route filters too.
    assert [hit["kind"] for hit in body["results"]] == ["journal"]


@pytest.mark.parametrize("kind", ["people", "skill", "shared"])
def test_a_kind_off_the_list_is_422(monkeypatch, kind: str) -> None:
    client = _client(monkeypatch)
    response = client.post(
        SEARCH_PATH, json={"query": "garden", "kinds": [kind]}, headers=_bearer(VIEWER)
    )
    assert response.status_code == 422
    assert client.store.calls == []


def test_limit_is_capped(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.post(
        SEARCH_PATH, json={"query": "garden", "limit": 1000}, headers=_bearer(VIEWER)
    )
    assert response.status_code == 422


def test_blank_query_returns_nothing(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.post(SEARCH_PATH, json={"query": "   "}, headers=_bearer(VIEWER))
    assert response.status_code == 200
    assert response.json() == {"results": []}
    assert client.store.calls == []


def test_no_embedding_model_is_503(monkeypatch) -> None:
    client = _client(monkeypatch, embed=False)
    response = client.post(SEARCH_PATH, json={"query": "garden"}, headers=_bearer(VIEWER))
    assert response.status_code == 503


def test_hit_shape(monkeypatch) -> None:
    client = _client(monkeypatch, rows=[_chunk("journal", "wiki/journal/2026/09/26/a.md")])
    hit = client.post(SEARCH_PATH, json={"query": "a"}, headers=_bearer(VIEWER)).json()["results"][
        0
    ]
    assert set(hit) == {"path", "title", "heading", "text", "kind", "score", "date"}
    assert hit["score"] == 0.9
