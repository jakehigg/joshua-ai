"""``POST /v1/internet``: the lookup core runs for the ``internet`` builtin of the gateway.

The internet agent is faked here: ``ask`` gives a canned answer, so what these
tests check is the route. The lookup itself is tested in
``test_internet_tool.py`` and the agent in ``test_internet.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from joshua_core.engine import internet as internet_agent
from joshua_core.engine.url_grants import UrlGrants
from joshua_core.engine.usage import Usage
from joshua_core.internet_api import build_internet_router
from joshua_core.main import build_app
from joshua_shared import config as config_module
from joshua_shared.contracts import INTERNET_PATH
from starlette.testclient import TestClient

GATEWAY = "gateway-tok"
CORE = "core-tok"

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    role: member
"""

# The answer of a lookup that read one page.
ANSWER = internet_agent.InternetAnswer(
    answer="The valve ships with a 1/2 inch thread.",
    sources=[internet_agent.Source(url="https://example.com/valve", title="The valve")],
    confidence="high",
)


def _settings(extra: str = "") -> config_module.JoshuaConfig:
    return config_module.parse(CONFIG + extra, env={}, source="<test>")


class FakeRepo:
    """The usage rows the route writes."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def add_usage_event(self, event: Any) -> None:
        self.events.append(event)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("JOSHUA_TOKEN_GATEWAY", GATEWAY)
    monkeypatch.setenv("JOSHUA_TOKEN_CORE", CORE)

    async def fake_ask(question: str, **kwargs: Any) -> Any:
        asked.append({"question": question, "urls": list(kwargs.get("urls") or [])})
        kwargs["usage_out"].append(Usage(input=120, output=40))
        return ANSWER

    asked: list[dict[str, Any]] = []
    monkeypatch.setattr(internet_agent, "ask", fake_ask)
    app = FastAPI()
    app.state.ctx = SimpleNamespace(
        settings=_settings(),
        repo=FakeRepo(),
        manager=SimpleNamespace(url_grants=UrlGrants()),
        internet_searcher=None,
    )
    app.include_router(build_internet_router())
    tc = TestClient(app)
    tc.ctx = app.state.ctx  # type: ignore[attr-defined]
    tc.asked = asked  # type: ignore[attr-defined]
    return tc


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _ask(client: TestClient, token: str | None = GATEWAY, **body: Any) -> Any:
    headers = _bearer(token) if token else {}
    return client.post(
        INTERNET_PATH,
        json={"question": "what thread does the valve have?", "person": "alex", **body},
        headers=headers,
    )


def test_the_core_app_serves_the_route() -> None:
    assert INTERNET_PATH in build_app().openapi()["paths"]


def test_a_lookup_comes_back_as_text_with_its_sources(client: TestClient) -> None:
    body = _ask(client).json()
    assert client.asked[0]["question"] == "what thread does the valve have?"
    assert "1/2 inch thread" in body["text"]
    assert "example.com/valve" in body["text"]
    # The text says what it is, so the caller never reads it as an instruction.
    assert "instruction" in body["text"]


def test_the_tokens_of_a_lookup_go_into_one_usage_row(client: TestClient) -> None:
    _ask(client, conversation="assignment:12")
    assert [event.name for event in client.ctx.repo.events] == ["internet"]
    assert client.ctx.repo.events[0].person_id == "alex"


def test_only_the_gateway_may_ask(client: TestClient) -> None:
    assert _ask(client, token=None).status_code == 401
    assert _ask(client, token="wrong").status_code == 401
    assert _ask(client, token=CORE).status_code == 403


def test_a_body_without_a_question_is_refused(client: TestClient) -> None:
    response = client.post(INTERNET_PATH, json={"person": "alex"}, headers=_bearer(GATEWAY))
    assert response.status_code == 400
    assert "question" in response.json()["error"]


def test_a_url_nobody_gave_is_refused_and_no_lookup_runs(client: TestClient) -> None:
    body = _ask(client, conversation="assignment:12", urls=["https://example.net/made-up"]).json()
    assert "were not read" in body["text"]
    assert client.asked == []


def test_a_source_of_an_earlier_answer_may_be_read_again(client: TestClient) -> None:
    _ask(client, conversation="assignment:12")
    body = _ask(client, conversation="assignment:12", urls=["https://example.com/valve"]).json()
    assert client.asked[1]["urls"] == ["https://example.com/valve"]
    assert "were not read" not in body["text"]
    # The grant is of that conversation alone.
    other = _ask(client, conversation="assignment:13", urls=["https://example.com/valve"]).json()
    assert "were not read" in other["text"]


def test_a_url_a_person_wrote_in_the_chat_may_be_read(client: TestClient) -> None:
    client.ctx.manager.url_grants.grant_from_text("c1", "read https://example.com/manual please")
    _ask(client, conversation="c1", urls=["https://example.com/manual"])
    assert client.asked[0]["urls"] == ["https://example.com/manual"]


def test_with_the_internet_off_the_answer_says_so(client: TestClient) -> None:
    client.ctx.settings = _settings("internet:\n  enabled: false\n")
    body = _ask(client).json()
    assert "turned off" in body["text"]
    assert client.asked == []
