"""The web search route, ``POST /v1/search``, against a fake SearXNG.

The fake is an ``httpx.MockTransport`` on ``app.state.search_transport``, so
these run with no network. Each refusal and each failure has a test, and the
logs and the answers never hold the query or the key.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import httpx2
import pytest
from conftest import TOKENS, bearer
from joshua_gateway import search
from joshua_gateway.main import build_app
from joshua_gateway.observability import CALL_LOG
from joshua_shared import config

KEY = "proxy-secret-value"

RESULTS = [
    {"title": "Braised short ribs", "url": "https://food.example/ribs", "content": "Braise."},
    {"title": "Again", "url": "https://food.example/ribs", "content": "a copy"},
    {"title": "Mail", "url": "mailto:someone@example.com"},
    {"title": "Script", "url": "javascript:alert(1)"},
    {"title": "File", "url": "file:///etc/passwd"},
    {"title": "Spaced", "url": "https://a.example/ b"},
    {"title": 5, "url": 5},
    "not an object",
    {"title": "  Long  title ", "url": "http://plain.example/x", "content": "x" * 5000},
]


def _yaml(extra: str = "", enabled: str = "", with_search: bool = True) -> str:
    text = (
        "name: Test\n"
        "timezone: America/New_York\n"
        "people:\n"
        "  - id: alex\n    name: Alex\n"
        "internet:\n"
        f"{enabled}"
        "  model: claude-sonnet-5\n"
    )
    if with_search:
        text += "  search:\n    url: http://searxng:8080/\n" + extra
    return text


class FakeSearxng:
    """A SearXNG that answers each request with ``status`` and ``body``."""

    def __init__(self, status: int = 200, body: Any = None, error: Exception | None = None):
        self.status = status
        self.body = {"query": "q", "results": RESULTS} if body is None else body
        self.error = error
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.error is not None:
                raise self.error
            if isinstance(self.body, bytes):
                return httpx.Response(self.status, content=self.body)
            return httpx.Response(self.status, json=self.body)

        return httpx.MockTransport(handle)


@pytest.fixture
def make_app(monkeypatch, config_path):
    for identity, token in TOKENS.items():
        monkeypatch.setenv(f"JOSHUA_TOKEN_{identity.upper()}", token)
    monkeypatch.setenv("SEARXNG_API_KEY", KEY)
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)

    def make(text: str, fake: FakeSearxng | None = None):
        config_path.write_text(text)
        monkeypatch.setattr(config, "_cache", None)
        app = build_app()
        app.state.search_transport = (fake or FakeSearxng()).transport()
        return app

    return make


async def _post(app, body, identity: str | None = "core", headers=None) -> httpx2.Response:
    transport = httpx2.ASGITransport(app=app)
    sent = {**(bearer(identity) if identity else {}), **(headers or {})}
    async with httpx2.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=30
    ) as client:
        if isinstance(body, bytes):
            return await client.post("/v1/search", headers=sent, content=body)
        return await client.post("/v1/search", headers=sent, json=body)


async def _search(make_app, fake: FakeSearxng, query: str = "short ribs", extra: str = ""):
    app = make_app(_yaml(extra), fake)
    return await _post(app, {"query": query})


def _entries() -> list[dict]:
    return [e for e in CALL_LOG.recent(limit=0) if e.get("search")]


# -- auth ------------------------------------------------------------------


async def test_no_bearer_is_401(make_app) -> None:
    assert (await _post(make_app(_yaml()), {"query": "a"}, None)).status_code == 401


async def test_a_wrong_bearer_is_401(make_app) -> None:
    response = await _post(make_app(_yaml()), {"query": "a"}, None, {"Authorization": "Bearer x"})
    assert response.status_code == 401


@pytest.mark.parametrize("identity", ["laptop", "ci", "channels", "gateway"])
async def test_a_caller_that_is_not_core_is_403(make_app, identity) -> None:
    fake = FakeSearxng()
    response = await _post(make_app(_yaml(), fake), {"query": "a"}, identity)
    assert response.status_code == 403
    assert fake.requests == []


async def test_a_person_header_does_not_open_the_route(make_app) -> None:
    app = make_app(_yaml())
    response = await _post(app, {"query": "a"}, "laptop", {"X-Joshua-Person": "alex"})
    assert response.status_code == 403


# -- a good call -----------------------------------------------------------


async def test_the_results_are_read_and_cleaned(make_app) -> None:
    response = await _search(make_app, FakeSearxng())
    assert response.status_code == 200
    results = response.json()["results"]
    assert [r["url"] for r in results] == ["https://food.example/ribs", "http://plain.example/x"]
    assert results[0] == {
        "title": "Braised short ribs",
        "url": "https://food.example/ribs",
        "snippet": "Braise.",
    }
    assert results[1]["title"] == "Long title"
    assert len(results[1]["snippet"]) <= 1000


async def test_the_request_is_the_json_api_with_the_query(make_app) -> None:
    fake = FakeSearxng()
    await _search(make_app, fake, extra="    categories: general\n    language: en\n")
    request = fake.requests[0]
    assert request.method == "GET"
    assert str(request.url).startswith("http://searxng:8080/search?")
    assert dict(request.url.params) == {
        "q": "short ribs",
        "format": "json",
        "categories": "general",
        "language": "en",
    }
    assert "authorization" not in request.headers


async def test_the_key_is_sent_as_a_bearer_only_when_it_is_set(make_app) -> None:
    fake = FakeSearxng()
    await _search(make_app, fake, extra="    api_key: ${SEARXNG_API_KEY:-}\n")
    assert fake.requests[0].headers["authorization"] == f"Bearer {KEY}"


async def test_max_results_caps_the_list(make_app) -> None:
    response = await _search(make_app, FakeSearxng(), extra="    max_results: 1\n")
    assert [r["url"] for r in response.json()["results"]] == ["https://food.example/ribs"]


async def test_an_empty_list_gives_no_result(make_app) -> None:
    response = await _search(make_app, FakeSearxng(body={"results": []}))
    assert response.status_code == 200
    assert response.json() == {"results": []}


async def test_the_logs_hold_no_query_and_no_key(make_app, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeSearxng(status=403, body={"error": KEY})
    await _search(
        make_app, fake, query="a private question", extra="    api_key: ${SEARXNG_API_KEY:-}\n"
    )
    await _search(make_app, FakeSearxng(), query="a private question")
    entries = _entries()[:2]
    assert entries[1]["status"] == "error"
    assert entries[0]["status"] == "success"
    assert entries[0]["query_chars"] == len("a private question")
    assert entries[0]["results"] == 2
    gateway_text = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("gateway"))
    assert "private" not in gateway_text
    assert KEY not in gateway_text
    assert KEY not in repr(_entries())


# -- failures --------------------------------------------------------------


async def test_a_403_says_that_the_json_format_is_off(make_app) -> None:
    response = await _search(make_app, FakeSearxng(status=403, body=b"Forbidden"))
    assert response.status_code == 502
    assert "search.formats" in response.json()["error"]
    assert "settings.yml" in response.json()["error"]


@pytest.mark.parametrize(
    ("fake", "status", "error"),
    [
        (FakeSearxng(status=401, body=b"no"), 502, "searxng refused the key (401)"),
        (FakeSearxng(status=500, body=b"boom"), 502, "searxng answered 500"),
        (FakeSearxng(body=b"<html>not json</html>"), 502, "searxng answer is not json"),
        (FakeSearxng(body=["a", "list"]), 502, "searxng answer has no results list"),
        (FakeSearxng(body={"results": "x"}), 502, "searxng answer has no results list"),
        (FakeSearxng(error=httpx.ReadTimeout("slow")), 504, "timeout"),
        (
            FakeSearxng(error=httpx.ConnectError("refused")),
            502,
            "searxng unreachable: ConnectError",
        ),
    ],
)
async def test_a_failure_gives_a_short_error(make_app, fake, status, error) -> None:
    response = await _search(make_app, fake, extra="    api_key: ${SEARXNG_API_KEY:-}\n")
    assert response.status_code == status
    assert response.json() == {"error": error}
    assert KEY not in response.text
    assert _entries()[0]["error"] == error


async def test_an_error_text_of_searxng_never_reaches_the_answer(make_app) -> None:
    body = {"error": f"https://user:{KEY}@search.example failed"}
    response = await _search(make_app, FakeSearxng(status=500, body=body))
    assert KEY not in response.text
    assert "search.example" not in response.text


async def test_no_search_block_is_404(make_app) -> None:
    fake = FakeSearxng()
    response = await _post(make_app(_yaml(with_search=False), fake), {"query": "a"})
    assert response.status_code == 404
    assert response.json()["error"] == "search not configured"
    assert fake.requests == []


async def test_the_internet_agent_turned_off_is_404(make_app) -> None:
    response = await _post(make_app(_yaml(enabled="  enabled: false\n")), {"query": "a"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 501},
        {"query": 5},
        {"query": "a", "url": "http://other.example"},
        {"query": "a", "categories": "files"},
        b"not json",
    ],
)
async def test_a_bad_body_is_400(make_app, body) -> None:
    fake = FakeSearxng()
    response = await _post(make_app(_yaml(), fake), body)
    assert response.status_code == 400
    assert fake.requests == []


# -- the parts ---------------------------------------------------------------


def test_the_params_hold_only_what_is_set() -> None:
    cfg = config.parse(_yaml(), {}, source="t.yaml").internet.search
    assert cfg is not None
    assert search.search_params(cfg, "q") == {"q": "q", "format": "json"}


def test_read_results_skips_a_result_with_no_url() -> None:
    body = json.loads(json.dumps({"results": [{"title": "t"}, {"url": "https://ok.example/"}]}))
    assert [r.url for r in search.read_results(body, 10)] == ["https://ok.example/"]
