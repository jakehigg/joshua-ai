"""``GET /v1/workspaces/options``: what a workspace may pick.

The route reads the config and one setting, so the tests build a config, give
the route a settings store of their own, and call it. The two instances are
the ones a person meets: one that names its models in ``workspaces.models``,
and one that names none, where the models of the assignment jobs are the
models of the instance.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from joshua_core.workspaces_api import (
    DEFAULT_SERVERS_SETTING,
    build_workspaces_router,
    parse_default_servers,
    workspace_models,
)
from joshua_shared import config as config_module
from starlette.testclient import TestClient

VIEWER = "viewer-token"

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
    role: member
"""

TWO_PEOPLE = """
  - id: sam
    name: Sam
    role: member
"""

SERVERS = """
mcp:
  files:
    kind: builtin
    description: The wiki and the attachments.
  tasks:
    type: stdio
    command: tasks
    allow:
      - alex
"""


TWO_MODELS = """
workspaces:
  models:
    - claude-opus-5
    - claude-sonnet-5
"""

NO_MODELS = """
workspaces:
  max_turns: 60
"""

ONE_ASSIGNMENT_MODEL = """
assignments:
  default_model: fast
  models:
    fast:
      model: little-model
      effort: low
"""


class Settings:
    """The settings store of core, as much of it as the route reads."""

    def __init__(self, default_servers: str | None = None) -> None:
        self.rows = {} if default_servers is None else {DEFAULT_SERVERS_SETTING: default_servers}

    async def get(self, key: str, *, decrypt: bool = True) -> Any:
        value = self.rows.get(key)
        return None if value is None else SimpleNamespace(key=key, value=value)


def _settings(extra: str = ""):
    return config_module.parse(CONFIG + extra, env={}, source="<test>")


def _client(extra: str = "", default_servers: str | None = None) -> TestClient:
    app = FastAPI()
    app.state.ctx = SimpleNamespace(
        settings=_settings(extra),
        repo=SimpleNamespace(settings=Settings(default_servers)),
    )
    app.include_router(build_workspaces_router())
    return TestClient(app)


def _options(extra: str = "", default_servers: str | None = None, person: str = "alex") -> Any:
    client = _client(extra, default_servers)
    answer = client.get(
        "/v1/workspaces/options",
        params={"person": person},
        headers={"Authorization": f"Bearer {VIEWER}"},
    )
    assert answer.status_code == 200
    return answer.json()


def _models(extra: str = "") -> list[dict[str, str]]:
    return _options(extra)["models"]


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOSHUA_TOKEN_VIEWER", VIEWER)


def test_the_models_of_the_instance_come_back_in_config_order() -> None:
    # The names of the assignment models are the names a person reads, and the
    # order is the order of the list.
    assert _models(TWO_MODELS) == [
        {"id": "claude-opus-5", "name": "opus"},
        {"id": "claude-sonnet-5", "name": "sonnet"},
    ]


def test_an_instance_that_names_no_model_still_lists_its_models() -> None:
    # With no list, the models of the assignment jobs are the models of the
    # instance: the key is the name, and ``model`` is the id.
    assert _models(NO_MODELS) == [
        {"id": "claude-sonnet-5", "name": "sonnet"},
        {"id": "claude-opus-5", "name": "opus"},
    ]
    assert _models(NO_MODELS + ONE_ASSIGNMENT_MODEL) == [{"id": "little-model", "name": "fast"}]


def test_a_model_with_no_name_of_its_own_reads_as_its_id() -> None:
    assert workspace_models(_settings("workspaces:\n  models:\n    - other-model\n")) == [
        {"id": "other-model", "name": "other-model"}
    ]


def test_one_model_under_two_names_is_one_entry() -> None:
    settings = _settings(
        """
assignments:
  default_model: sonnet
  models:
    sonnet:
      model: claude-sonnet-5
    quick:
      model: claude-sonnet-5
"""
    )
    assert workspace_models(settings) == [{"id": "claude-sonnet-5", "name": "sonnet"}]


# The servers a new workspace starts with: the owner's setting, kept to what
# the person at the dialog may use, and the servers of each other person so
# the dialog intersects them for a shared workspace.


def test_a_new_workspace_starts_with_the_servers_the_owner_set() -> None:
    answer = _options(SERVERS, "files, tasks")
    assert answer["default_servers"] == ["files", "tasks"]
    assert [server["name"] for server in answer["servers"]] == ["files", "tasks"]


def test_no_setting_pre_picks_no_server() -> None:
    # The behaviour of an instance whose owner has set nothing.
    assert _options(SERVERS)["default_servers"] == []
    assert _options(SERVERS, "")["default_servers"] == []


def test_a_server_the_person_cannot_use_is_never_pre_picked() -> None:
    # ``tasks`` is scoped to alex, so sam's dialog neither offers it nor
    # pre-picks it: the create would refuse it.
    answer = _options(TWO_PEOPLE + SERVERS, "files, tasks", person="sam")
    assert answer["default_servers"] == ["files"]
    assert [server["name"] for server in answer["servers"]] == ["files"]


def test_a_name_no_server_of_the_instance_holds_is_dropped() -> None:
    assert _options(SERVERS, "files, weather")["default_servers"] == ["files"]


def test_each_person_carries_the_servers_of_a_shared_pre_pick() -> None:
    # The dialog of a shared workspace intersects the pre-pick over its
    # people, which is the rule the create applies: alex reaches ``tasks`` and
    # sam does not, so a shared workspace with sam in it starts with ``files``
    # alone.
    answer = _options(TWO_PEOPLE + SERVERS, "files, tasks")
    assert answer["default_servers"] == ["files", "tasks"]
    assert answer["people"] == [{"id": "sam", "name": "Sam", "servers": ["files"]}]
    shared = [
        name
        for name in answer["default_servers"]
        if all(name in person["servers"] for person in answer["people"])
    ]
    assert shared == ["files"]


def test_the_setting_reads_as_a_list_of_names() -> None:
    assert parse_default_servers(" files , tasks , files ") == ["files", "tasks"]
    assert parse_default_servers("") == []
