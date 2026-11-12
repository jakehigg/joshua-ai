"""Offline tests for the JSON route of the search.

``GET /api/v1/search?q=<query>`` answers both halves: ``meaning``, which core
answers, and ``words``, a substring match over the Markdown the person can
read. A MockTransport stands in for core, so no network and no model are
needed. The route answers 200 with no core, with a core that fails, and with
an empty query.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest
from joshua_gateway import viewer
from joshua_gateway import viewer_search as search_mod
from joshua_gateway.viewer_api import search as search_api
from joshua_shared import config
from joshua_shared.contracts import SearchHit, SearchResponse
from starlette.testclient import TestClient

ALEX = ("alex", "alex-secret")
MIA = ("mia", "mia-secret")
VIEWER_TOKEN = "viewer-token"

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
      - id: mia
        name: Mia
        role: guest
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
        mia: ${VIEWER_PW_MIA:-}
    """)


def _hit(path: str, *, text: str = "Oats with honey.", title: str = "Breakfast", **kw) -> dict:
    return {
        "path": path,
        "title": title,
        "heading": kw.get("heading", ""),
        "text": text,
        "kind": kw.get("kind", "journal"),
        "score": 0.8,
        "date": kw.get("date", "2026-09-04"),
    }


@pytest.fixture
def data(monkeypatch, tmp_path) -> Path:
    root = tmp_path / "data"
    (root / "wiki").mkdir(parents=True)
    (root / "wiki/pizza.md").write_text("# Pizza\n\nDough and sauce.\n")
    cfg_path = tmp_path / "joshua.yaml"
    cfg_path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(cfg_path))
    monkeypatch.setenv("JOSHUA_DATA_DIR", str(root))
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX[1])
    monkeypatch.setenv("VIEWER_PW_MIA", MIA[1])
    monkeypatch.delenv(search_mod.CORE_URL_ENV, raising=False)
    monkeypatch.delenv(search_mod.VIEWER_TOKEN_ENV, raising=False)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setattr(config, "_cache_people_file_mtime", None)
    return root


class FakeCore:
    """Records each request and answers with ``status`` and ``results``."""

    def __init__(self, results: list[dict], status: int = 200) -> None:
        self.results = results
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "no"})
        return httpx.Response(200, json={"results": self.results})


def _with_core(monkeypatch, core: FakeCore) -> None:
    transport = httpx.MockTransport(core.handler)
    monkeypatch.setattr(
        search_mod,
        "from_env",
        lambda env: search_mod.CoreSearch("http://core", VIEWER_TOKEN, transport=transport),
    )


def _search(query: str, auth: tuple[str, str] = ALEX) -> Any:
    response = TestClient(viewer.build_app()).get("/api/v1/search", params={"q": query}, auth=auth)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_an_empty_query_answers_both_halves_empty_and_calls_core_not_at_all(
    data, monkeypatch
) -> None:
    core = FakeCore([])
    _with_core(monkeypatch, core)
    body = _search("   ")
    assert body == {"query": "", "meaning_configured": True, "meaning": [], "words": []}
    assert core.requests == []


def test_a_word_hit_carries_the_page_title_and_the_line(data) -> None:
    body = _search("Dough")
    assert body["query"] == "Dough"
    assert body["words"] == [
        {"url": "/wiki/pizza.md", "label": "Pizza", "snippet": "Dough and sauce."}
    ]


def test_the_words_half_stops_at_the_maximum(data) -> None:
    for index in range(search_api.SEARCH_MAX_RESULTS + 10):
        (data / "wiki" / f"page-{index:03d}.md").write_text("Dough again.\n")
    assert len(_search("Dough")["words"]) == search_api.SEARCH_MAX_RESULTS


def test_without_core_the_answer_is_the_words_half_alone(data) -> None:
    body = _search("Dough")
    assert body["meaning_configured"] is False
    assert body["meaning"] is None
    assert body["words"]


def test_a_member_names_no_person_for_the_wiki_and_the_member_for_the_documents(
    data, monkeypatch
) -> None:
    core = FakeCore([_hit("wiki/journal/2026/09/04/oatmeal.md")])
    _with_core(monkeypatch, core)
    body = _search("oats")
    assert body["meaning"] == [
        {
            "url": "/wiki/journal/2026/09/04/oatmeal.md",
            "label": "Breakfast",
            "source": "journal · 2026-09-04",
            "snippet": "Oats with honey.",
        }
    ]
    bodies = {}
    for request in core.requests:
        assert request.url.path == search_mod.SEARCH_PATH
        assert request.headers["authorization"] == f"Bearer {VIEWER_TOKEN}"
        assert not any(key.lower().startswith("x-joshua") for key in request.headers)
        sent = json.loads(request.content)
        bodies[sent["source"]] = sent
    assert set(bodies) == {"files", "documents"}
    assert bodies["files"]["query"] == "oats"
    assert bodies["files"]["person"] is None
    assert bodies["documents"]["person"] == "alex"


def test_a_guest_names_no_person_and_asks_for_no_document(data, monkeypatch) -> None:
    core = FakeCore([])
    _with_core(monkeypatch, core)
    assert _search("oats", auth=MIA)["meaning"] == []
    [request] = core.requests
    assert json.loads(request.content)["source"] == "files"
    assert "mia" not in request.content.decode()


def test_a_document_hit_links_the_document_page(data, monkeypatch) -> None:
    doc = _hit(
        "documents/alex/party-plan.md",
        title="Party plan",
        text="Food and music.",
        kind="document",
        heading="plan, active",
        date=None,
    )
    _with_core(monkeypatch, FakeCore([doc]))
    [hit] = _search("party")["meaning"]
    assert hit["url"] == "/documents/alex/party-plan"
    assert hit["label"] == "Party plan"
    assert hit["source"] == "document · plan, active"


def test_a_core_call_that_fails_leaves_the_meaning_half_out_and_keeps_the_words(
    data, monkeypatch
) -> None:
    _with_core(monkeypatch, FakeCore([], status=503))
    body = _search("Dough")
    assert body["meaning_configured"] is True
    assert body["meaning"] is None
    assert [hit["url"] for hit in body["words"]] == ["/wiki/pizza.md"]


def test_a_path_the_viewer_does_not_link_is_left_out() -> None:
    response = SearchResponse(
        results=[SearchHit(**_hit("people/alex/attachments/scan.pdf", kind="wiki"))]
    )
    assert search_api.meaning_json(response) == []


def test_a_long_passage_is_cut() -> None:
    response = SearchResponse(results=[SearchHit(**_hit("wiki/a.md", text="x" * 400))])
    [hit] = search_api.meaning_json(response)
    assert len(hit["snippet"]) == search_api.SNIPPET_CHARS + 1
    assert hit["snippet"].endswith("…")
