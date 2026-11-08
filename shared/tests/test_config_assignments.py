"""Validation of `assignments`: the container for long jobs in the background."""

from __future__ import annotations

import pytest
from joshua_shared import config

BASE = """
name: Home
timezone: America/New_York
people:
  - id: alex
    name: Alex
  - id: sam
    name: Sam
    role: guest
"""

FULL = """
assignments:
  enabled: true
  default_model: opus
  models:
    sonnet:
      model: claude-sonnet-5
      effort: medium
    opus:
      model: claude-opus-5
      effort: high
  default_max_turns: 150
  default_working_time: 2h
  limits:
    five_hour_pct: 60
    seven_day_pct: 80
    max_five_hour_pct: 90
    max_seven_day_pct: 95
    when_unknown: run
  max_concurrent: 2
  slice_turns: 5
  keep_failed_work: 3d
  repos:
    - gitlab.example.net/infra/*
    - github.com/example-org/app
  platforms:
    gitlab.example.net:
      kind: gitlab
      token_env: ASSIGNMENTS_GITLAB_TOKEN
  people:
    alex:
      notify: imessage:dm:alex
"""


def parse(extra: str = "") -> config.JoshuaConfig:
    return config.parse(BASE + extra, {}, source="t.yaml")


def refused(extra: str) -> str:
    with pytest.raises(config.ConfigError) as exc:
        parse(extra)
    return str(exc.value)


def test_no_section_is_off_with_the_defaults() -> None:
    section = parse().assignments
    assert section.enabled is False
    assert section.default_model == "sonnet"
    assert set(section.models) == {"sonnet", "opus"}
    assert section.models["sonnet"].model == "claude-sonnet-5"
    assert section.default_working_seconds == 4 * 3600
    assert section.keep_failed_work_seconds == 7 * 86400
    assert section.limits.five_hour_pct == 70
    assert section.limits.max_five_hour_pct == 90
    assert section.limits.when_unknown == "refuse"
    assert section.max_concurrent == 1 and section.slice_turns == 10
    assert section.repos == [] and section.platforms == {} and section.people == {}


def test_an_empty_section_takes_the_defaults() -> None:
    assert parse("assignments:\n").assignments.enabled is False


def test_a_full_section_validates() -> None:
    section = parse(FULL).assignments
    assert section.enabled is True
    assert section.default_model == "opus"
    assert section.models["opus"].effort == "high"
    assert section.default_max_turns == 150
    assert section.default_working_seconds == 7200
    assert section.limits.when_unknown == "run"
    assert section.platforms["gitlab.example.net"].token_env == "ASSIGNMENTS_GITLAB_TOKEN"
    assert section.people["alex"].notify == "imessage:dm:alex"


def test_a_file_that_names_models_replaces_the_builtin_models() -> None:
    section = parse(
        "assignments:\n  default_model: fast\n  models:\n    fast:\n      model: claude-haiku-5\n"
    ).assignments
    assert set(section.models) == {"fast"}


@pytest.mark.parametrize(
    "token",
    ["ghp_0123456789abcdef", "github_pat_0123456789", "AKIAABCDEFGHIJKLMNOP"],
)
def test_a_literal_token_in_a_platform_is_refused(token: str) -> None:
    message = refused(
        "assignments:\n  platforms:\n    gitlab.example.net:\n"
        f"      kind: gitlab\n      token_env: {token}\n"
    )
    assert "holds a literal token" in message
    assert "assignments.platforms.gitlab.example.net.token_env" in message
    assert token not in message


def test_a_literal_credential_anywhere_is_refused() -> None:
    message = refused(
        "assignments:\n  platforms:\n    gitlab.example.net:\n"
        "      kind: gitlab\n      token_env: glpat-0123456789abcdef\n"
    )
    assert "glpat-0123456789abcdef" not in message


def test_a_token_through_the_environment_is_refused_too() -> None:
    text = BASE + "assignments:\n  people:\n    alex:\n      notify: ${NOTIFY}\n"
    with pytest.raises(config.ConfigError) as exc:
        config.parse(text, {"NOTIFY": "ghp_0123456789abcdef"}, source="t.yaml")
    assert "literal token" in str(exc.value)


def test_token_env_must_name_a_variable() -> None:
    message = refused(
        "assignments:\n  platforms:\n    gitlab.example.net:\n"
        "      kind: gitlab\n      token_env: not a name\n"
    )
    assert "environment variable" in message


def test_a_default_model_not_in_models_is_refused() -> None:
    assert "not in models" in refused("assignments:\n  default_model: haiku\n")


def test_an_empty_model_map_is_refused() -> None:
    assert "at least one model" in refused("assignments:\n  models:\n")


def test_a_bad_model_name_is_refused() -> None:
    message = refused(
        "assignments:\n  default_model: Big\n  models:\n    Big:\n      model: claude-opus-5\n"
    )
    assert "not a valid model name" in message


@pytest.mark.parametrize(
    ("key", "maximum"),
    [("five_hour_pct", "max_five_hour_pct"), ("seven_day_pct", "max_seven_day_pct")],
)
def test_a_ceiling_above_its_maximum_is_refused(key: str, maximum: str) -> None:
    message = refused(f"assignments:\n  limits:\n    {key}: 99\n    {maximum}: 90\n")
    assert f"{key} is above {maximum}" in message


def test_a_percent_above_100_is_refused() -> None:
    refused("assignments:\n  limits:\n    max_five_hour_pct: 101\n")


def test_when_unknown_is_refuse_or_run() -> None:
    refused("assignments:\n  limits:\n    when_unknown: maybe\n")


@pytest.mark.parametrize("value", ["0h", "four hours", "4"])
def test_a_bad_duration_is_refused(value: str) -> None:
    refused(f"assignments:\n  default_working_time: {value}\n")


@pytest.mark.parametrize(
    "pattern", ["gitlab.example.net/infra", "GitLab.example.net/a/b", "example/a/b", "a b/c/d"]
)
def test_a_bad_repo_pattern_is_refused(pattern: str) -> None:
    assert "host/group/name" in refused(f"assignments:\n  repos:\n    - {pattern}\n")


def test_a_subgroup_repo_pattern_is_valid() -> None:
    section = parse("assignments:\n  repos:\n    - gitlab.example.net/a/b/c\n").assignments
    assert section.repos == ["gitlab.example.net/a/b/c"]


def test_a_platform_host_must_be_lower_case() -> None:
    message = refused(
        "assignments:\n  platforms:\n    GitLab.example.net:\n"
        "      kind: gitlab\n      token_env: TOKEN\n"
    )
    assert "lowercase" in message


def test_a_person_must_be_in_people() -> None:
    message = refused("assignments:\n  people:\n    bob:\n      notify: telegram:dm:bob\n")
    assert "unknown person id 'bob'" in message


def test_an_empty_notify_is_none() -> None:
    section = parse("assignments:\n  people:\n    alex:\n      notify: ''\n").assignments
    assert section.people["alex"].notify is None


def test_the_commented_block_in_the_example_file_validates() -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "joshua.example.yaml").read_text()
    start = text.index("# assignments:")
    block = text[start:].split("\n\n", 1)[0]
    body = "\n".join(line[2:] for line in block.splitlines())
    section = parse(body).assignments
    assert section.enabled is False
    assert section.repos == ["gitlab.example.net/infra/*"]
    assert section.people["alex"].notify == "imessage:dm:alex"
