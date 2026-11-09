"""Server-side tool filtering: tools/list allowlist and call-time deny."""

from __future__ import annotations

import logging

from conftest import echo_mcp, gateway_session
from joshua_gateway.main import lifespan
from joshua_gateway.observability import CALL_LOG
from joshua_gateway.proxy import upstream_streams  # noqa: F401 — module under test


async def test_list_filters_to_allowlist(gateway):
    app = gateway()
    async with lifespan(app):
        async with gateway_session(app, "/echo", "core") as session:
            names = [t.name for t in (await session.list_tools()).tools]
    assert names == ["echo"]


async def test_allowed_tool_call_succeeds(gateway):
    app = gateway()
    async with lifespan(app):
        async with gateway_session(app, "/echo", "core") as session:
            result = await session.call_tool("echo", {"message": "hi"})
    assert result.is_error is False
    assert result.content[0].text == "echo: hi"


async def test_filtered_tool_denied_at_call_time(gateway):
    app = gateway()
    async with lifespan(app):
        async with gateway_session(app, "/echo", "core") as session:
            result = await session.call_tool("secret_tool", {})
    assert result.is_error is True
    denied = [c for c in CALL_LOG.recent(tool="secret_tool") if c["status"] == "denied"]
    assert denied, "a denied call must be logged"


async def test_unmatched_allow_pattern_warns_at_connect(gateway, caplog):
    app = gateway(echo_mcp(tools_allow="nonexistent_*"))
    with caplog.at_level(logging.WARNING, logger="gateway.upstream"):
        async with lifespan(app):
            pass
    warned = [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict)
        and r.msg.get("message") == "tool allow patterns match no upstream tool"
    ]
    assert warned, "an allow pattern that matches nothing must warn"
    assert warned[0]["server"] == "echo"
    assert warned[0]["patterns"] == ["nonexistent_*"]


def _refusals(caplog) -> list[dict]:
    return [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("message") == "tool call refused"
    ]


async def test_a_refused_call_is_logged_with_its_reason_and_no_text(gateway, caplog):
    app = gateway(echo_mcp(tools_allow="'*'"))
    body = "the private body of a comment"
    with caplog.at_level(logging.INFO, logger="gateway.upstream"):
        async with lifespan(app):
            headers = {"X-Joshua-Person": "alex"}
            async with gateway_session(app, "/echo", "core", headers) as session:
                await session.call_tool("refuse", {"reason": "nothing_applied", "message": body})
                await session.call_tool("refuse", {"reason": f"Free text: {body}"})
                await session.call_tool("echo", {"message": body})
    lines = _refusals(caplog)
    assert [line["reason"] for line in lines] == ["nothing_applied", "other"]
    assert all(
        (line["tool"], line["person"], line["server"], line["identity"])
        == ("refuse", "alex", "echo", "core")
        for line in lines
    )
    assert all(
        set(line) == {"message", "identity", "person", "server", "tool", "reason"} for line in lines
    )
    assert body not in caplog.text


def test_refusal_reason_reads_the_house_style() -> None:
    import mcp_types as types
    from joshua_gateway.proxy import refusal_reason

    def text(value: str):
        return types.CallToolResult(content=[types.TextContent(type="text", text=value)])

    assert refusal_reason(text('{"status": "rejected", "reason": "busy"}')) == "busy"
    assert refusal_reason(text('{"status": "running", "reason": "busy"}')) is None
    assert refusal_reason(text('{"status": "rejected"}')) is None
    assert refusal_reason(text('{"status": "rejected", "reason": 5}')) == "other"
    assert refusal_reason(text("{not json")) is None
    assert refusal_reason(text("plain")) is None
    structured = types.CallToolResult(
        content=[], structured_content={"status": "rejected", "reason": "not_found"}
    )
    assert refusal_reason(structured) == "not_found"
