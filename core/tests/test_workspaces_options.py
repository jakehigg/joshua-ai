"""``GET /v1/workspaces/options``: the models a workspace may pick.

The route needs no database: it answers from the config alone, so the tests
build a config and call the route. The two instances are the ones a person
meets: one that names its models in ``workspaces.models``, and one that names
none, where the models of the assignment jobs are the models of the instance.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from joshua_core.workspaces_api import build_workspaces_router, workspace_models
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


def _settings(extra: str = ""):
    return config_module.parse(CONFIG + extra, env={}, source="<test>")


def _client(extra: str = "") -> TestClient:
    app = FastAPI()
    app.state.ctx = SimpleNamespace(settings=_settings(extra), repo=None)
    app.include_router(build_workspaces_router())
    return TestClient(app)


def _models(extra: str = "") -> list[dict[str, str]]:
    client = _client(extra)
    answer = client.get(
        "/v1/workspaces/options",
        params={"person": "alex"},
        headers={"Authorization": f"Bearer {VIEWER}"},
    )
    assert answer.status_code == 200
    return answer.json()["models"]


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
