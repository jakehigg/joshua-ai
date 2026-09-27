"""Offline tests for the viewer's search by meaning.

A MockTransport stands in for core, so no network and no model are needed. The
fixture shape is the same as in `test_viewer.py`.
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
from joshua_shared.contracts import SearchHit, SearchResponse
from starlette.testclient import TestClient

ALEX_PW = "alex-secret"
ALEX = ("alex", ALEX_PW)
VIEWER_TOKEN = "viewer-token"

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


def _with_core(monkeypatch, core: FakeCore) -> None:
    transport = httpx.MockTransport(core.handler)
    monkeypatch.setattr(
        search_mod,
        "from_env",
        lambda env: search_mod.CoreSearch("http://core", VIEWER_TOKEN, transport=transport),
    )


def test_without_core_the_page_is_words_only(data) -> None:
    html = TestClient(viewer.build_app()).get("/search", params={"q": "Dough"}, auth=ALEX).text
    assert "/wiki/pizza.md" in html
    assert "By meaning" not in html
    assert "By words" not in html


def test_meaning_section_comes_first_and_links_the_page(data, monkeypatch) -> None:
    core = FakeCore([_hit("wiki/journal/2026/09/04/oatmeal.md")])
    _with_core(monkeypatch, core)
    html = TestClient(viewer.build_app()).get("/search", params={"q": "Dough"}, auth=ALEX).text
    assert html.index("By meaning") < html.index("By words")
    assert 'href="/wiki/journal/2026/09/04/oatmeal.md"' in html
    assert "journal · 2026-09-04" in html
    assert "/wiki/pizza.md" in html  # the words section is still there


def test_the_call_carries_the_viewer_token_and_names_no_person(data, monkeypatch) -> None:
    core = FakeCore([])
    _with_core(monkeypatch, core)
    TestClient(viewer.build_app()).get("/search", params={"q": "oats"}, auth=ALEX)
    [request] = core.requests
    assert request.url.path == search_mod.SEARCH_PATH
    assert request.headers["authorization"] == f"Bearer {VIEWER_TOKEN}"
    body = json.loads(request.content)
    assert body["query"] == "oats"
    assert "alex" not in request.content.decode()
    assert not any(k.lower().startswith("x-joshua") for k in request.headers)


def test_core_down_says_so_and_keeps_words(data, monkeypatch) -> None:
    _with_core(monkeypatch, FakeCore([], status=503))
    html = TestClient(viewer.build_app()).get("/search", params={"q": "Dough"}, auth=ALEX).text
    assert "Search by meaning is not available now." in html
    assert "/wiki/pizza.md" in html


def test_core_unreachable_says_so(data, monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    transport = httpx.MockTransport(refuse)
    monkeypatch.setattr(
        search_mod,
        "from_env",
        lambda env: search_mod.CoreSearch("http://core", VIEWER_TOKEN, transport=transport),
    )
    html = TestClient(viewer.build_app()).get("/search", params={"q": "Dough"}, auth=ALEX).text
    assert "Search by meaning is not available now." in html


def test_passage_html_is_escaped(data, monkeypatch) -> None:
    core = FakeCore([_hit("wiki/x.md", text="<script>alert(1)</script>", title="<b>t</b>")])
    _with_core(monkeypatch, core)
    html = TestClient(viewer.build_app()).get("/search", params={"q": "x"}, auth=ALEX).text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>t</b>" not in html


def test_a_path_outside_the_wiki_is_not_linked() -> None:
    assert search_mod.viewer_url("people/alex/attachments/2026/09/scan.pdf") is None
    assert search_mod.viewer_url("shared/old.md") is None
    assert search_mod.viewer_url("wiki/../people/alex.md") is None
    assert search_mod.viewer_url("wiki/a b.md") == "/wiki/a%20b.md"
    response = SearchResponse(
        results=[
            SearchHit(**_hit("people/alex/attachments/scan.pdf", kind="wiki")),
        ]
    )
    assert "scan.pdf" not in search_mod.meaning_html(response)


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
