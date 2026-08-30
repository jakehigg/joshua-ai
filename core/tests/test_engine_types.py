"""Direct tests for the SDK-free types and the stub session."""

from __future__ import annotations

from pathlib import Path

from joshua_core.engine.stub_agent import ECHO_TOOLS_MARKER, StubAgentSession
from joshua_core.engine.types import Attachment, TurnResult


def test_attachment_and_turn_result_defaults():
    att = Attachment(path="attachments/2026/08/x.jpg", mime="image/jpeg", name="x.jpg")
    assert att.path.endswith("x.jpg")
    result = TurnResult(text="hi")
    assert result.tools_used == []
    assert result.tool_calls == []
    assert result.is_error is False


async def test_stub_echo_tools_marker():
    session = StubAgentSession("sys", Path("/tmp"), "c1", server_names=["files", "scheduling"])
    result = await session.run(f"list {ECHO_TOOLS_MARKER}")
    assert result.text == "tools: files, scheduling"
    assert session.connected


async def test_stub_resume_fails_once_then_succeeds():
    session = StubAgentSession("sys", Path("/tmp"), "c1", resume="bogus")
    try:
        await session.run("hi")
        raise AssertionError("expected the first resumed run to fail")
    except RuntimeError:
        pass
    result = await session.run("hi")
    assert result.text.startswith("(stub)")
    await session.close()
    assert not session.connected
