"""The web search route, ``POST /v1/search``.

The config is the allowlist. Each refusal rule has a negative test here. A
call proves the reading of each result shape, the timeout, and a call log
entry that holds no query. The upstream is the ``search_server.py`` fixture
over stdio, on an entry with ``allow:`` and no value, so no person reaches it.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import httpx2
import mcp_types as types
import pytest
from conftest import TOKENS, bearer
from joshua_gateway import search
from joshua_gateway.main import build_app, lifespan
from joshua_gateway.observability import CALL_LOG
from joshua_shared import config

FIXTURE = Path(__file__).parent / "fixtures" / "search_server.py"


def _yaml(tool: str = "search_list", extra: str = "", policy: str = "", enabled: str = "") -> str:
    return (
        "name: Test\n"
        "timezone: America/New_York\n"
        "people:\n"
        "  - id: alex\n    name: Alex\n"
        "internet:\n"
        f"{enabled}"
        "  search:\n"
        "    server: websearch\n"
        f"    tool: {tool}\n"
        f"{extra}"
        "mcp:\n"
        "  websearch:\n"
        f"    type: stdio\n    command: {sys.executable}\n    args:\n      - {FIXTURE}\n"
        "    allow:\n"
        f"{policy}"
    )


@pytest.fixture
def make_app(monkeypatch, config_path):
    for identity, token in TOKENS.items():
        monkeypatch.setenv(f"JOSHUA_TOKEN_{identity.upper()}", token)
    monkeypatch.delenv("ADMIN_CALLERS", raising=False)

    def make(text: str):
        config_path.write_text(text)
        monkeypatch.setattr(config, "_cache", None)
        return build_app()

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


async def _search(make_app, tool: str, query: str = "short ribs", **kwargs) -> httpx2.Response:
    app = make_app(_yaml(tool, **kwargs))
    async with lifespan(app):
        return await _post(app, {"query": query})


def _entries() -> list[dict]:
    return [e for e in CALL_LOG.recent(limit=0) if e.get("search")]


# -- auth ------------------------------------------------------------------


async def test_no_bearer_is_401(make_app) -> None:
    app = make_app(_yaml())
    async with lifespan(app):
        assert (await _post(app, {"query": "a"}, None)).status_code == 401


async def test_a_wrong_bearer_is_401(make_app) -> None:
    app = make_app(_yaml())
    async with lifespan(app):
        response = await _post(app, {"query": "a"}, None, {"Authorization": "Bearer wrong"})
    assert response.status_code == 401


@pytest.mark.parametrize("identity", ["laptop", "ci", "channels", "gateway"])
async def test_a_caller_that_is_not_core_is_403(make_app, identity) -> None:
    app = make_app(_yaml())
    async with lifespan(app):
        response = await _post(app, {"query": "a"}, identity)
    assert response.status_code == 403


async def test_a_person_header_does_not_open_the_route(make_app) -> None:
    """A person never reaches the route: only the core identity does."""
    app = make_app(_yaml())
    async with lifespan(app):
        response = await _post(app, {"query": "a"}, "laptop", {"X-Joshua-Person": "alex"})
    assert response.status_code == 403


async def test_no_person_reaches_the_search_server_through_mcp(make_app) -> None:
    """`allow:` with no value: the tool is the internet agent's, and no person's."""
    app = make_app(_yaml())
    transport = httpx2.ASGITransport(app=app)
    async with lifespan(app):
        async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as c:
            for person in ("alex", "unknown", None):
                headers = {**bearer("core")}
                if person:
                    headers["X-Joshua-Person"] = person
                assert (await c.get("/websearch", headers=headers)).status_code == 403


# -- a good call -----------------------------------------------------------


async def test_a_json_list_is_read_and_cleaned(make_app) -> None:
    response = await _search(make_app, "search_list")
    assert response.status_code == 200
    results = response.json()["results"]
    urls = [r["url"] for r in results]
    assert urls == ["https://args.example/", "https://two.example/page", "https://three.example/"]
    assert results[1] == {
        "title": "Second",
        "url": "https://two.example/page",
        "snippet": "short ribs",
    }
    assert len(results[2]["snippet"]) <= 1000


async def test_the_query_and_the_fixed_arguments_reach_the_tool(make_app) -> None:
    extra = "    query_arg: q\n    args:\n      count: '3'\n      safe: 'true'\n      lang: en\n"
    response = await _search(make_app, "search_list", extra=extra)
    sent = json.loads(response.json()["results"][0]["snippet"])
    assert sent == {"q": "short ribs", "count": 3, "safe": True, "lang": "en"}


async def test_max_results_caps_the_list(make_app) -> None:
    response = await _search(make_app, "search_list", extra="    max_results: 1\n")
    assert [r["url"] for r in response.json()["results"]] == ["https://args.example/"]


async def test_a_nested_object_is_read(make_app) -> None:
    response = await _search(make_app, "search_nested")
    assert response.json()["results"] == [
        {"title": "Nested", "url": "https://nested.example/a", "snippet": ""}
    ]


async def test_one_json_object_for_each_part_is_read(make_app) -> None:
    response = await _search(make_app, "search_parts")
    assert [r["url"] for r in response.json()["results"]] == [
        "https://one.example/",
        "https://two.example/",
    ]


async def test_structured_content_is_read(make_app) -> None:
    response = await _search(make_app, "search_structured")
    assert [r["url"] for r in response.json()["results"]] == ["https://s.example/"]


async def test_plain_text_gives_the_url_of_each_block(make_app) -> None:
    response = await _search(make_app, "search_text")
    results = response.json()["results"]
    assert [r["url"] for r in results] == [
        "https://food.example/short-ribs",
        "https://cook.example/basics",
    ]
    assert results[0]["title"] == "Braised short ribs"
    assert results[0]["snippet"] == "Braise at 160 C for three hours."
    # A URL in the text of a block is words, and it is not a result.
    assert "https://other.example/in-text" not in [r["url"] for r in results]


async def test_garbage_gives_no_result(make_app) -> None:
    response = await _search(make_app, "search_garbage")
    assert response.status_code == 200
    assert response.json() == {"results": []}


async def test_the_call_log_holds_no_query(make_app, caplog) -> None:
    caplog.set_level(logging.INFO)
    await _search(make_app, "search_list", query="a private question")
    entry = _entries()[0]
    assert entry["status"] == "success"
    assert entry["identity"] == "core"
    assert entry["query_chars"] == len("a private question")
    assert entry["results"] == 3
    assert "private" not in repr(_entries())
    assert "private" not in caplog.text


# -- failures and refusals -------------------------------------------------


async def test_a_tool_error_is_502_and_holds_no_upstream_text(make_app) -> None:
    response = await _search(make_app, "search_fail")
    assert response.status_code == 502
    assert response.json() == {"error": "search tool error"}
    assert "PROVIDER-SECRET-VALUE" not in response.text


async def test_a_slow_tool_times_out(make_app) -> None:
    response = await _search(make_app, "search_slow", extra="    timeout_seconds: 0.5\n")
    assert response.status_code == 504
    assert _entries()[0]["error"] == "timeout"


async def test_an_upstream_that_breaks_is_502(make_app, monkeypatch) -> None:
    async def broken(up, cfg, query):
        raise RuntimeError("connection to https://user:secret@search.example failed")

    monkeypatch.setattr(search, "call_search", broken)
    response = await _search(make_app, "search_list")
    assert response.status_code == 502
    assert "secret" not in response.text
    assert _entries()[0]["error"] == "RuntimeError"


async def test_an_upstream_that_is_not_serving_is_503(make_app) -> None:
    app = make_app(_yaml())
    async with lifespan(app):
        app.state.upstreams.pop("websearch")
        response = await _post(app, {"query": "a"})
    assert response.status_code == 503


@pytest.mark.parametrize(
    ("tool", "policy", "status", "reason"),
    [
        ("search_wipe", "", 403, "tool_destructive"),
        ("no_such_tool", "", 403, "tool_not_exposed"),
        ("search_list", "    tools:\n      deny:\n        - search_*\n", 403, "tool_not_exposed"),
        (
            "search_list",
            "    tools:\n      class:\n        search_list: act\n",
            403,
            "tool_class_act",
        ),
        (
            "search_list",
            "    tools:\n      class:\n        search_*: write-local\n",
            403,
            "tool_class_write-local",
        ),
    ],
)
async def test_a_refused_tool_is_not_called(make_app, tool, policy, status, reason) -> None:
    response = await _search(make_app, tool, policy=policy)
    assert response.status_code == status
    assert response.json()["error"] == reason
    assert _entries()[0]["status"] == "denied"


async def test_no_search_block_is_404(make_app) -> None:
    text = _yaml().replace("internet:\n  search:\n    server: websearch\n", "x-cut:\n")
    text = text.replace("x-cut:\n    tool: search_list\n", "")
    app = make_app(text)
    async with lifespan(app):
        response = await _post(app, {"query": "a"})
    assert response.status_code == 404
    assert response.json()["error"] == "search not configured"


async def test_the_internet_agent_turned_off_is_404(make_app) -> None:
    app = make_app(_yaml(enabled="  enabled: false\n"))
    async with lifespan(app):
        response = await _post(app, {"query": "a"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 501},
        {"query": 5},
        {"query": "a", "tool": "search_wipe"},
        {"query": "a", "args": {"count": 99}},
        b"not json",
    ],
)
async def test_a_bad_body_is_400(make_app, body) -> None:
    app = make_app(_yaml())
    async with lifespan(app):
        response = await _post(app, body)
    assert response.status_code == 400


# -- the parts ---------------------------------------------------------------


def _result(text: str = "", structured=None) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], structured_content=structured
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"items": [{"title": "I", "link": "https://i.example/"}]},
        {"organic": [{"title": "O", "url": "https://o.example/"}]},
        {"data": {"results": [{"url": "https://d.example/"}]}},
        [[{"url": "https://deep.example/"}]],
        {"url": "https://single.example/"},
    ],
)
def test_each_list_key_is_read(payload) -> None:
    assert len(search.normalize(_result(json.dumps(payload)), 10)) == 1


def test_one_result_on_each_line_is_read() -> None:
    lines = "\n".join(json.dumps({"url": f"https://{n}.example/"}) for n in ("a", "b"))
    assert len(search.normalize(_result(lines), 10)) == 2


def test_text_with_one_url_on_each_line_is_read() -> None:
    text = "First page https://a.example/x.\nSecond page https://b.example/y\nno url here"
    results = search.normalize(_result(text), 10)
    assert [r.url for r in results] == ["https://a.example/x", "https://b.example/y"]
    assert results[0].snippet.startswith("First page")


def test_a_url_with_space_or_another_scheme_is_dropped() -> None:
    payload = [{"url": "https://a.example/ b"}, {"url": "file:///etc/passwd"}, {"url": 5}]
    assert search.normalize(_result(json.dumps(payload)), 10) == []


def test_an_empty_answer_gives_no_result() -> None:
    assert search.normalize(types.CallToolResult(content=[]), 10) == []


def test_a_value_that_does_not_convert_stays_text() -> None:
    schema = {"properties": {"count": {"type": "integer"}, "f": {"type": "number"}}}
    assert search.coerce_args({"count": "many", "f": "1.5", "x": "y"}, schema) == {
        "count": "many",
        "f": 1.5,
        "x": "y",
    }
    assert search.coerce_args({"a": "1"}, None) == {"a": "1"}
