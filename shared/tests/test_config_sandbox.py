"""Validation of `sandbox`: the code sandbox of the `sandbox` builtin."""

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


def test_no_section_turns_the_sandbox_off() -> None:
    sandbox = parse().sandbox
    assert sandbox.enabled is False
    assert sandbox.url == "http://sandbox:8888"
    assert sandbox.run_timeout_s == 120
    assert sandbox.max_kernels == 4


def test_an_empty_section_takes_the_defaults() -> None:
    assert parse("sandbox:\n").sandbox.enabled is False


def test_a_full_section_validates() -> None:
    sandbox = parse(
        "sandbox:\n"
        "  enabled: true\n"
        "  url: http://joshua-sandbox:8888/\n"
        "  run_timeout_s: 30\n"
        "  max_output_chars: 5000\n"
        "  max_input_bytes: 1048576\n"
        "  max_kernels: 2\n"
        "  folder_ttl_hours: 6\n"
    ).sandbox
    assert sandbox.enabled is True
    assert sandbox.url == "http://joshua-sandbox:8888"
    assert (sandbox.run_timeout_s, sandbox.max_kernels, sandbox.folder_ttl_hours) == (30, 2, 6)


@pytest.mark.parametrize(
    "extra",
    [
        "sandbox:\n  url: sandbox:8888\n",
        "sandbox:\n  run_timeout_s: 1\n",
        "sandbox:\n  run_timeout_s: 3600\n",
        "sandbox:\n  max_kernels: 0\n",
        "sandbox:\n  unknown: 1\n",
    ],
)
def test_a_bad_value_is_refused(extra: str) -> None:
    with pytest.raises(config.ConfigError):
        parse(extra)


def test_sandbox_is_a_builtin_name() -> None:
    cfg = parse("mcp:\n  sandbox:\n    kind: builtin\n    allow: all\nsandbox:\n  enabled: true\n")
    assert cfg.mcp["sandbox"].kind == "builtin"
    assert config.SANDBOX_OFF_WARNING not in cfg.warnings


def test_the_server_with_the_sandbox_off_is_a_warning() -> None:
    cfg = parse("mcp:\n  sandbox:\n    kind: builtin\n    allow: all\n")
    assert config.SANDBOX_OFF_WARNING in cfg.warnings
