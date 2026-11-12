"""The ``workspaces`` section: the defaults, the callers, and the checks."""

from __future__ import annotations

import pytest
from joshua_shared import config
from joshua_shared.config import ConfigError

BASE = """
name: Test
timezone: America/New_York
people:
  - id: alex
    name: Alex
"""


def load(section: str = ""):
    return config.parse(BASE + section, {}, source="test.yaml")


def test_the_defaults() -> None:
    ws = load().workspaces
    assert ws.allowed_callers == ["viewer", "gateway"]
    assert ws.context_cap == 150_000
    assert ws.tool_search_max_servers == 3
    assert ws.models == []
    assert ws.viewer_url is None


def test_an_empty_section_and_an_empty_caller_list_are_valid() -> None:
    assert load("workspaces:\n").workspaces.context_cap == 150_000
    assert load("workspaces:\n  allowed_callers:\n").workspaces.allowed_callers == []


def test_values_are_read_and_callers_are_normalised() -> None:
    ws = load(
        "workspaces:\n  allowed_callers:\n    - ' Viewer '\n    - mcp\n  context_cap: 90000\n"
        "  tool_search_max_servers: 0\n  models:\n    - m1\n"
        "  viewer_url: https://joshua.example.org\n"
    ).workspaces
    assert ws.allowed_callers == ["viewer", "mcp"]
    assert (ws.context_cap, ws.tool_search_max_servers, ws.models) == (90000, 0, ["m1"])
    assert ws.viewer_url == "https://joshua.example.org"


@pytest.mark.parametrize(
    "section",
    [
        "workspaces:\n  context_cap: 10\n",
        "workspaces:\n  tool_search_max_servers: -1\n",
        "workspaces:\n  viewer_url: ftp://x\n",
        "workspaces:\n  unknown: 1\n",
    ],
)
def test_bad_values_are_refused(section: str) -> None:
    with pytest.raises(ConfigError):
        load(section)


def test_an_empty_viewer_url_means_none() -> None:
    assert load('workspaces:\n  viewer_url: ""\n').workspaces.viewer_url is None
