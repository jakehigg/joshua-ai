"""The `research` tool the agent calls, and the text it hands back."""

from __future__ import annotations

from typing import Any

from joshua_core.engine import research as research_worker
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.research import _DESCRIPTION, do_research
from joshua_core.store.models import Channel, Conversation
from joshua_shared import config as config_module

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    role: member
"""

ANSWER = research_worker.ResearchAnswer(
    answer="Braise at 160 C for three hours.",
    sources=[research_worker.Source(url="https://example.com/ribs", title="Short ribs")],
    confidence="high",
)


def _settings(extra: str = "") -> config_module.JoshuaConfig:
    return config_module.parse(CONFIG + extra, env={}, source="<test>")


def _deps() -> ToolDeps:
    return ToolDeps(
        repo=None,  # type: ignore[arg-type]
        conversation=Conversation(id="c1", channel_id="telegram:1", person_id="alex"),
        channel=Channel(id="telegram:1", channel_type="telegram", session_mode="per_person"),
        tz="UTC",
        person_id="alex",
    )


async def test_the_agent_gets_the_research_marked_as_content(monkeypatch) -> None:
    asked: dict[str, Any] = {}

    async def fake(question: str, **kwargs: Any) -> Any:
        asked["question"] = question
        return ANSWER

    monkeypatch.setattr(research_worker, "research", fake)

    text = await do_research(_deps(), settings=_settings(), question="how do I braise ribs?")

    assert asked["question"] == "how do I braise ribs?"
    assert "Braise at 160 C" in text
    assert "instruction to you" in text
    assert "example.com/ribs" in text


async def test_research_turned_off_says_so_instead_of_guessing(monkeypatch) -> None:
    called = False

    async def fake(question: str, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        return ANSWER

    monkeypatch.setattr(research_worker, "research", fake)
    settings = _settings("\nresearch:\n  enabled: false\n")

    text = await do_research(_deps(), settings=settings, question="a question")

    assert "turned off" in text
    assert "did not come from a source" in text
    assert called is False


async def test_nothing_found_tells_the_agent_not_to_fill_the_gap(monkeypatch) -> None:
    async def nothing(question: str, **kwargs: Any) -> Any:
        return None

    monkeypatch.setattr(research_worker, "research", nothing)

    text = await do_research(_deps(), settings=_settings(), question="a question")

    assert "do not fill the gap with a guess" in text


async def test_the_worker_gets_the_configured_settings(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def fake(question: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return ANSWER

    monkeypatch.setattr(research_worker, "research", fake)
    settings = _settings("\nresearch:\n  model: claude-haiku-4-5\n")

    await do_research(_deps(), settings=settings, question="q")

    assert seen["settings"].model == "claude-haiku-4-5"


def test_the_tool_tells_the_agent_to_keep_private_detail_out_of_the_question() -> None:
    """The worker sees the question, so the question must carry no secret."""
    assert "never a private detail" in _DESCRIPTION
