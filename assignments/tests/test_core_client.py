"""The calls to core: the limits and the usage, with the retry rules."""

from __future__ import annotations

import httpx
import pytest
from conftest import make_settings
from joshua_assignments.core_client import CoreClient
from joshua_shared.contracts import UsageWriteRow
from sdk_fakes import FakeCore


def row() -> UsageWriteRow:
    return UsageWriteRow(kind="assignment", run_id=3, ref="3:slice:1")


async def test_limits_reads_the_answer() -> None:
    core = FakeCore(five_hour=33.0)
    answer = await core.client(make_settings()).limits()
    assert answer is not None and answer.five_hour is not None
    assert answer.five_hour.used_pct == 33.0


@pytest.mark.parametrize("status", [404, 500])
async def test_limits_that_cannot_be_read_are_none(status) -> None:
    core = FakeCore()
    core.limit_status = status
    assert await core.client(make_settings()).limits() is None


async def test_an_answer_that_is_not_valid_or_an_error_is_none() -> None:
    def handler(request):
        return httpx.Response(200, json={"five_hour": 3})

    client = CoreClient(
        make_settings(),
        make_client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert await client.limits() is None

    def broken(request):
        raise httpx.ConnectError("down")

    client = CoreClient(
        make_settings(),
        make_client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(broken)),
    )
    assert await client.limits() is None


async def test_post_usage_retries_a_5xx_and_a_connection_error_but_not_a_4xx() -> None:
    calls: list[int] = []
    replies: list = [httpx.ConnectError("down"), 502, 200]

    def handler(request):
        calls.append(1)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(reply, json={})

    client = CoreClient(
        make_settings(),
        make_client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        backoff_s=0,
    )
    assert await client.post_usage(row()) is True
    assert len(calls) == 3

    core = FakeCore()
    core.usage_replies = [400]
    assert await core.client(make_settings()).post_usage(row()) is False
    assert len(core.usage_requests) == 1

    core.usage_replies = [500, 500, 500]
    assert await core.client(make_settings()).post_usage(row()) is False


async def test_no_core_url_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="CORE_URL"):
        await CoreClient(make_settings(core_url=None)).limits()
