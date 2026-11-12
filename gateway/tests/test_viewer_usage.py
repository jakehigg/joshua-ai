"""Offline tests for the client of the usage by day.

A MockTransport stands in for core, so no network is needed. The route that
calls this client is ``viewer_api.usage`` (`test_viewer_api_usage.py`), and
the page is React.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from joshua_gateway import viewer_usage as usage_mod
from joshua_shared.contracts import USAGE_DEFAULT_DAYS, USAGE_MAX_DAYS

VIEWER_TOKEN = "viewer-token"

ANSWER: dict = {
    "timezone": "America/New_York",
    "days": 30,
    "groups": ["chat"],
    "rows": [
        {
            "date": "2026-10-20",
            "groups": {"chat": {"calls": 2, "input": 10, "output": 5, "total": 15}},
        }
    ],
    "totals": {"chat": {"calls": 2, "input": 10, "output": 5, "total": 15}},
}


Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> usage_mod.CoreUsage:
    transport = httpx.MockTransport(handler)
    return usage_mod.CoreUsage("http://core", VIEWER_TOKEN, transport=transport)


async def test_the_call_carries_the_fleet_token_and_the_window() -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=ANSWER)

    body = await _client(answer).fetch(7)
    assert body is not None and body.groups == ["chat"]
    assert body.rows[0].groups["chat"].total == 15
    [request] = sent
    assert request.url.path == usage_mod.USAGE_PATH
    assert request.url.params["days"] == "7"
    assert request.headers["authorization"] == f"Bearer {VIEWER_TOKEN}"


async def test_a_refused_call_is_none() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "no"})

    assert await _client(refuse).fetch(30) is None


async def test_a_network_error_is_none() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    assert await _client(boom).fetch(30) is None


async def test_an_unknown_shape_is_none() -> None:
    def odd(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rows": "nope"})

    assert await _client(odd).fetch(30) is None


def test_from_env_needs_both_values() -> None:
    assert usage_mod.from_env({}) is None
    assert usage_mod.from_env({usage_mod.CORE_URL_ENV: "http://core"}) is None
    assert usage_mod.from_env({usage_mod.VIEWER_TOKEN_ENV: "t"}) is None
    both = {usage_mod.CORE_URL_ENV: "http://core", usage_mod.VIEWER_TOKEN_ENV: "t"}
    assert isinstance(usage_mod.from_env(both), usage_mod.CoreUsage)


def test_days_is_held_to_the_window_core_allows() -> None:
    assert usage_mod.parse_days(None) == USAGE_DEFAULT_DAYS
    assert usage_mod.parse_days("") == USAGE_DEFAULT_DAYS
    assert usage_mod.parse_days("x") == USAGE_DEFAULT_DAYS
    assert usage_mod.parse_days("7") == 7
    assert usage_mod.parse_days("0") == 1
    assert usage_mod.parse_days("9999") == USAGE_MAX_DAYS


def test_a_count_reads_as_a_person_reads_it() -> None:
    assert usage_mod.exact(1234567) == "1 234 567"
    assert [usage_mod.short(n) for n in (950, 12_400, 3_400_000, 2_000_000)] == [
        "950",
        "12k",
        "3.4M",
        "2M",
    ]
