"""The `internet` tool the agent calls, and the text it hands back."""

from __future__ import annotations

from typing import Any

from joshua_core.engine import internet as internet_agent
from joshua_core.engine.tools import ToolDeps
from joshua_core.engine.tools.internet import _DESCRIPTION, do_lookup
from joshua_core.engine.url_grants import UrlGrants
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

ANSWER = internet_agent.InternetAnswer(
    answer="Braise at 160 C for three hours.",
    sources=[internet_agent.Source(url="https://example.com/ribs", title="Short ribs")],
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


async def test_the_agent_gets_the_answer_marked_as_content(monkeypatch) -> None:
    asked: dict[str, Any] = {}

    async def fake(question: str, **kwargs: Any) -> Any:
        asked["question"] = question
        return ANSWER

    monkeypatch.setattr(internet_agent, "ask", fake)

    text = await do_lookup(_deps(), settings=_settings(), question="how do I braise ribs?")

    assert asked["question"] == "how do I braise ribs?"
    assert "Braise at 160 C" in text
    assert "instruction to you" in text
    assert "example.com/ribs" in text


async def test_the_internet_turned_off_says_so_instead_of_guessing(monkeypatch) -> None:
    called = False

    async def fake(question: str, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        return ANSWER

    monkeypatch.setattr(internet_agent, "ask", fake)
    settings = _settings("\ninternet:\n  enabled: false\n")

    text = await do_lookup(_deps(), settings=settings, question="a question")

    assert "turned off" in text
    assert "did not come from a source" in text
    assert called is False


async def test_nothing_found_tells_the_agent_not_to_fill_the_gap(monkeypatch) -> None:
    async def nothing(question: str, **kwargs: Any) -> Any:
        return None

    monkeypatch.setattr(internet_agent, "ask", nothing)

    text = await do_lookup(_deps(), settings=_settings(), question="a question")

    assert "do not fill the gap with a guess" in text


async def test_the_worker_gets_the_configured_settings(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def fake(question: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return ANSWER

    monkeypatch.setattr(internet_agent, "ask", fake)
    settings = _settings("\ninternet:\n  model: claude-haiku-4-5\n")

    await do_lookup(_deps(), settings=settings, question="q")

    assert seen["settings"].model == "claude-haiku-4-5"


def test_the_tool_tells_the_agent_to_keep_private_detail_out_of_the_question() -> None:
    """The worker sees the question, so the question must carry no secret."""
    assert "never a private detail" in _DESCRIPTION


# ── URLs the agent may pass ──────────────────────────────────────────────────


def _grants(conversation_id: str = "c1", text: str = "") -> UrlGrants:
    grants = UrlGrants()
    if text:
        grants.grant_from_text(conversation_id, text)
    return grants


async def test_a_link_the_person_sent_is_read(monkeypatch) -> None:
    """ "Save this recipe for me" with a link in the message."""
    seen: dict[str, Any] = {}

    async def fake(question: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return ANSWER

    monkeypatch.setattr(internet_agent, "ask", fake)
    grants = _grants(text="save this recipe for me https://example.com/ribs")

    text = await do_lookup(
        _deps(),
        settings=_settings(),
        question="save this recipe",
        urls=["https://example.com/ribs"],
        grants=grants,
    )

    assert seen["urls"] == ["https://example.com/ribs"]
    assert "Braise at 160 C" in text


async def test_a_url_nobody_gave_is_refused(monkeypatch) -> None:
    """A page can tell the agent to fetch something. This is where that stops."""
    called = False

    async def fake(question: str, **kwargs: Any) -> Any:
        nonlocal called
        called = True
        return ANSWER

    monkeypatch.setattr(internet_agent, "ask", fake)

    text = await do_lookup(
        _deps(),
        settings=_settings(),
        question="read this",
        urls=["https://attacker.example/?data=secret"],
        grants=_grants(text="what is for dinner?"),
    )

    assert "were not read" in text
    assert "ask them to send the link" in text
    assert called is False


async def test_a_source_of_an_answer_can_be_followed_up(monkeypatch) -> None:
    """ "Look at that page again and see what it says about X"."""
    seen: list[dict[str, Any]] = []

    async def fake(question: str, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return ANSWER

    monkeypatch.setattr(internet_agent, "ask", fake)
    grants = _grants(text="how do I braise short ribs?")

    await do_lookup(_deps(), settings=_settings(), question="ribs", grants=grants)
    # The answer's source is granted, so the next turn may name it.
    await do_lookup(
        _deps(),
        settings=_settings(),
        question="does that page say anything about wine?",
        urls=["https://example.com/ribs"],
        grants=grants,
    )

    assert seen[1]["urls"] == ["https://example.com/ribs"]


async def test_one_good_url_and_one_bad_one_reads_the_good_one(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def fake(question: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return ANSWER

    monkeypatch.setattr(internet_agent, "ask", fake)
    grants = _grants(text="see https://example.com/ok")

    text = await do_lookup(
        _deps(),
        settings=_settings(),
        question="q",
        urls=["https://example.com/ok", "https://attacker.example/x"],
        grants=grants,
    )

    assert seen["urls"] == ["https://example.com/ok"]
    assert "were not read" in text


async def test_with_no_register_no_url_is_read(monkeypatch) -> None:
    """With nothing recording what a person gave, nothing is granted."""

    async def fake(question: str, **kwargs: Any) -> Any:
        return ANSWER

    monkeypatch.setattr(internet_agent, "ask", fake)

    text = await do_lookup(
        _deps(),
        settings=_settings(),
        question="q",
        urls=["https://example.com/x"],
        grants=None,
    )

    assert "were not read" in text


async def test_a_question_with_no_url_is_unaffected(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def fake(question: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return ANSWER

    monkeypatch.setattr(internet_agent, "ask", fake)

    await do_lookup(_deps(), settings=_settings(), question="q", grants=_grants())

    assert seen["urls"] == []
