"""Offline tests for the client of the search by meaning.

A MockTransport stands in for core, so no network and no model are needed. The
route that calls this client is ``viewer_api.search``
(`test_viewer_api_search.py`).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import httpx
import pytest
from joshua_gateway import viewer
from joshua_gateway import viewer_search as search_mod
from joshua_shared import config
from starlette.testclient import TestClient

ALEX_PW = "alex-secret"

CONFIG = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
    viewer:
      enabled: true
      users:
        alex: ${VIEWER_PW_ALEX:-}
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
    monkeypatch.setenv("VIEWER_PW_ALEX", ALEX_PW)
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


async def test_documents_merge_by_score_and_a_failed_documents_call_is_left_out() -> None:
    wiki = {**_hit("wiki/a.md"), "score": 0.7}
    doc = {**_hit("documents/alex/b.md", kind="document", date=None), "score": 0.9}

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"results": [doc] if body["source"] == "documents" else [wiki]}
        )

    client = search_mod.CoreSearch("http://core", "t", transport=httpx.MockTransport(answer))
    response = await client.search("x", person="alex")
    assert response is not None
    assert [h.path for h in response.results] == ["documents/alex/b.md", "wiki/a.md"]

    def no_documents(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["source"] == "documents":
            return httpx.Response(500)
        return httpx.Response(200, json={"results": [wiki]})

    client = search_mod.CoreSearch("http://core", "t", transport=httpx.MockTransport(no_documents))
    response = await client.search("x", person="alex")
    assert response is not None and [h.path for h in response.results] == ["wiki/a.md"]


async def test_a_refused_search_is_none() -> None:
    core = FakeCore([], status=503)
    client = search_mod.CoreSearch("http://core", "t", transport=httpx.MockTransport(core.handler))
    assert await client.search("oats") is None


async def test_an_unreachable_core_is_none() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = search_mod.CoreSearch("http://core", "t", transport=httpx.MockTransport(refuse))
    assert await client.search("oats") is None


def test_a_path_outside_the_wiki_is_not_linked() -> None:
    assert search_mod.viewer_url("people/alex/attachments/2026/09/scan.pdf") is None
    assert search_mod.viewer_url("shared/old.md") is None
    assert search_mod.viewer_url("wiki/../people/alex.md") is None
    assert search_mod.viewer_url("wiki/a b.md") == "/wiki/a%20b.md"
    assert search_mod.viewer_url("documents/alex/plan.md") == "/documents/alex/plan"
    assert search_mod.viewer_url("documents/alex/../mia/plan.md") is None
    assert search_mod.viewer_url("documents/plan.md") is None
    assert search_mod.viewer_url("documents/alex/page.html") is None


def test_from_env_needs_the_url_and_the_token() -> None:
    assert search_mod.from_env({}) is None
    assert search_mod.from_env({"CORE_URL": "http://core"}) is None
    assert search_mod.from_env({"JOSHUA_TOKEN_VIEWER": "t"}) is None
    assert search_mod.from_env({"CORE_URL": "http://core", "JOSHUA_TOKEN_VIEWER": "t"})


async def test_a_query_too_long_for_core_is_not_sent() -> None:
    core = FakeCore([])
    client = search_mod.CoreSearch("http://core", "t", transport=httpx.MockTransport(core.handler))
    assert await client.search("x" * 5000) is None
    assert core.requests == []


async def test_an_unknown_answer_shape_is_none() -> None:
    def odd(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"path": 1}]})

    client = search_mod.CoreSearch("http://core", "t", transport=httpx.MockTransport(odd))
    assert await client.search("oats") is None


def test_readyz_reports_core_search(data, monkeypatch) -> None:
    client = TestClient(viewer.build_app())
    assert client.get("/readyz").json()["core_search"] is False
    monkeypatch.setenv(search_mod.CORE_URL_ENV, "http://core")
    monkeypatch.setenv(search_mod.VIEWER_TOKEN_ENV, "t")
    body = client.get("/readyz")
    assert body.json()["core_search"] is True
    assert "t" not in body.json().values()
