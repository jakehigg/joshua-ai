"""The config of the routes that the assignments container calls:
``inference.forwarder``, ``inference.assignment``, ``usage.writers``,
``memory.exclude_folders``, and ``gateway.person_header_callers``."""

from __future__ import annotations

import pytest
from joshua_shared import config
from joshua_shared.config import ConfigError
from joshua_shared.inference import job_model_setting, resolve_inference

BASE = """
name: Test
timezone: America/New_York
people:
  - id: alex
    name: Alex
core:
  model: chat-model
"""
ENV = {"SERVER_KEY": "server-key-value"}


def load(section: str = ""):
    return config.parse(BASE + section, ENV, source="test.yaml")


def test_the_defaults_name_the_assignments_container() -> None:
    cfg = load()
    assert cfg.inference.forwarder.allowed_callers == ["assignments"]
    assert cfg.usage.writers == ["assignments"]
    assert cfg.usage.allowed_callers == ["viewer"]
    assert cfg.memory.exclude_folders == ["assignments"]
    assert cfg.gateway.person_header_callers == ["core", "assignments"]


def test_an_empty_value_turns_the_forwarder_and_the_write_off() -> None:
    cfg = load(
        "inference:\n  forwarder:\n    allowed_callers:\n"
        "usage:\n  writers:\n"
        "memory:\n  exclude_folders:\n"
    )
    assert cfg.inference.forwarder.allowed_callers == []
    assert cfg.usage.writers == []
    assert cfg.memory.exclude_folders == []


def test_an_empty_section_keeps_the_defaults() -> None:
    cfg = load("inference:\n  forwarder:\ngateway:\n")
    assert cfg.inference.forwarder.allowed_callers == ["assignments"]
    assert cfg.gateway.person_header_callers == ["core", "assignments"]


def test_caller_names_are_lower_case() -> None:
    cfg = load(
        "inference:\n  forwarder:\n    allowed_callers:\n      - Assignments\n"
        "usage:\n  writers:\n    - ' Assignments '\n"
        "gateway:\n  person_header_callers:\n    - CORE\n    - core\n"
    )
    assert cfg.inference.forwarder.allowed_callers == ["assignments"]
    assert cfg.usage.writers == ["assignments"]
    assert cfg.gateway.person_header_callers == ["core"]


@pytest.mark.parametrize("section", ["    - assignments\n", "    - viewer\n"])
def test_the_person_header_list_must_hold_core(section: str) -> None:
    with pytest.raises(ConfigError, match="must hold core"):
        load("gateway:\n  person_header_callers:\n" + section)


def test_an_empty_person_header_list_is_refused() -> None:
    with pytest.raises(ConfigError):
        load("gateway:\n  person_header_callers:\n")


def test_the_assignment_job_uses_the_core_model_by_default() -> None:
    cfg = load()
    assert job_model_setting(cfg, "assignment") == "chat-model"
    resolved = resolve_inference(cfg, "assignment", oauth_token="oauth")
    assert resolved.env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth"
    assert resolved.tool_search is False


def test_the_assignment_block_sends_the_job_to_another_server() -> None:
    cfg = load(
        "inference:\n  assignment:\n    base_url: http://llm.example.net:4000\n"
        "    api_key: ${SERVER_KEY}\n    model: local-model\n"
    )
    resolved = resolve_inference(cfg, "assignment", oauth_token="oauth")
    assert resolved.env["ANTHROPIC_BASE_URL"] == "http://llm.example.net:4000"
    assert resolved.env["ANTHROPIC_AUTH_TOKEN"] == "server-key-value"
    assert resolved.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert resolved.model == "local-model"
    assert resolved.endpoint == "llm.example.net:4000"


def test_the_assignment_block_cannot_set_tool_search() -> None:
    with pytest.raises(ConfigError, match="tool_search"):
        load("inference:\n  assignment:\n    tool_search: true\n")
