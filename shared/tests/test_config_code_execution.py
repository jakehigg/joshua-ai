"""Validation of `code_execution`: the settings of the `code-execution` builtin."""

from __future__ import annotations

import pytest
from joshua_shared import config

BASE = """
name: Home
timezone: America/New_York
people:
  - id: alex
    name: Alex
"""


def parse(extra: str = "") -> config.JoshuaConfig:
    return config.parse(BASE + extra, {}, source="t.yaml")


def test_no_section_turns_code_execution_off() -> None:
    code_execution = parse().code_execution
    assert code_execution.enabled is False
    assert code_execution.url == "http://code-execution:8888"
    assert code_execution.run_timeout_s == 120
    assert code_execution.max_kernels == 4


def test_an_empty_section_takes_the_defaults() -> None:
    assert parse("code_execution:\n").code_execution.enabled is False


def test_a_full_section_validates() -> None:
    code_execution = parse(
        "code_execution:\n"
        "  enabled: true\n"
        "  url: http://joshua-code-execution:8888/\n"
        "  run_timeout_s: 30\n"
        "  max_output_chars: 5000\n"
        "  max_input_bytes: 1048576\n"
        "  max_kernels: 2\n"
        "  folder_ttl_hours: 6\n"
    ).code_execution
    assert code_execution.enabled is True
    assert code_execution.url == "http://joshua-code-execution:8888"
    assert (
        code_execution.run_timeout_s,
        code_execution.max_kernels,
        code_execution.folder_ttl_hours,
    ) == (30, 2, 6)


@pytest.mark.parametrize(
    "extra",
    [
        "code_execution:\n  url: code-execution:8888\n",
        "code_execution:\n  run_timeout_s: 1\n",
        "code_execution:\n  run_timeout_s: 3600\n",
        "code_execution:\n  max_kernels: 0\n",
        "code_execution:\n  unknown: 1\n",
    ],
)
def test_a_bad_value_is_refused(extra: str) -> None:
    with pytest.raises(config.ConfigError):
        parse(extra)


def test_code_execution_is_a_builtin_name() -> None:
    cfg = parse(
        "mcp:\n  code-execution:\n    kind: builtin\n    allow: all\n"
        "code_execution:\n  enabled: true\n"
    )
    assert cfg.mcp["code-execution"].kind == "builtin"
    assert config.CODE_EXECUTION_OFF_WARNING not in cfg.warnings


def test_the_server_with_code_execution_off_is_a_warning() -> None:
    cfg = parse("mcp:\n  code-execution:\n    kind: builtin\n    allow: all\n")
    assert config.CODE_EXECUTION_OFF_WARNING in cfg.warnings
