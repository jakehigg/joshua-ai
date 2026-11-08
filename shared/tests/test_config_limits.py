"""Validation of `limits`: the subscription limits route and tool, and the
`LimitsResponse` contract of `GET /v1/limits`."""

from __future__ import annotations

import pytest
from joshua_shared import config
from joshua_shared.contracts import LIMITS_PATH, LimitsResponse

BASE = """
name: Home
timezone: America/New_York
people:
  - id: alex
    name: Alex
"""


def parse(extra: str = "") -> config.JoshuaConfig:
    return config.parse(BASE + extra, {}, source="t.yaml")


def test_no_section_takes_the_defaults() -> None:
    limits = parse().limits
    assert limits.enabled is True
    assert limits.max_age == "5m" and limits.max_age_seconds == 300
    assert limits.chat_tool is True and limits.voice_tool is True
    assert limits.tool_jobs() == frozenset({"chat", "voice"})


def test_an_empty_section_takes_the_defaults() -> None:
    assert parse("limits:\n").limits.enabled is True


def test_a_full_section_validates() -> None:
    limits = parse(
        "limits:\n  enabled: true\n  max_age: 2m\n  chat_tool: false\n  voice_tool: true\n"
    ).limits
    assert limits.max_age_seconds == 120
    assert limits.tool_jobs() == frozenset({"voice"})


def test_disabled_gives_the_tool_to_no_job() -> None:
    limits = parse("limits:\n  enabled: false\n").limits
    assert limits.tool_jobs() == frozenset()


@pytest.mark.parametrize("value", ["0m", "five", "5"])
def test_a_bad_max_age_is_refused(value: str) -> None:
    with pytest.raises(config.ConfigError):
        parse(f"limits:\n  max_age: {value}\n")


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(config.ConfigError):
        parse("limits:\n  fresh: true\n")


def test_the_contract_round_trips() -> None:
    body = {
        "available": True,
        "reason": None,
        "source": "usage_api",
        "observed_at": "2026-11-07T20:00:00-05:00",
        "stale": False,
        "five_hour": {
            "used_pct": 23.0,
            "left_pct": 77.0,
            "resets_at": "2026-11-07T22:30:00-05:00",
            "status": "normal",
        },
        "seven_day": None,
        "scoped": [{"name": "Fable", "used_pct": 13.0, "left_pct": 87.0, "resets_at": None}],
        "extra_usage": {"enabled": True, "used": 0.0, "limit": 25.0, "currency": "USD"},
        "retry_after": None,
    }
    assert LimitsResponse.model_validate(body).model_dump() == body
    assert LIMITS_PATH == "/v1/limits"


def test_the_contract_refuses_an_unknown_source() -> None:
    with pytest.raises(ValueError):
        LimitsResponse.model_validate({"available": True, "source": "guess"})
