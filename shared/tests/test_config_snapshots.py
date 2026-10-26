"""Validation of `memory.snapshots`: the config is the allowlist, so each
rejection here is a snapshot that never runs."""

from __future__ import annotations

import textwrap

import pytest
from joshua_shared import config
from joshua_shared.config import Snapshot, parse_duration

BASE = """
name: Home
timezone: America/New_York
people:
  - id: alex
    name: Alex
  - id: mia
    name: Mia
mcp:
  tasks:
    type: http
    url: https://tasks.example.com/mcp
    allow: all
  mine:
    type: http
    url: https://mine.example.com/mcp
    allow:
      - alex
"""


def parse(snapshots: str) -> config.JoshuaConfig:
    block = textwrap.indent(textwrap.dedent(snapshots), "    ")
    return config.parse(BASE + "memory:\n  snapshots:\n" + block, {}, source="t.yaml")


ONE = """
- name: open-tasks
  server: tasks
  tool: list_tasks
  every: 5m
"""


def test_default_is_empty() -> None:
    assert config.parse(BASE, {}, source="t.yaml").memory.snapshots == []


def test_a_full_entry_validates() -> None:
    cfg = parse(
        """
        - name: open-tasks
          server: tasks
          tool: list_tasks
          args:
            list: home
          every: 30m
          scope: alex
          items: result.tasks
          max_chars: 5000
          max_age: 2h
        """
    )
    (snap,) = cfg.memory.snapshots
    assert snap.args == {"list": "home"}
    assert snap.every_seconds == 1800
    assert snap.person_id == "alex"
    assert snap.items == "result.tasks"
    assert snap.max_chars == 5000
    assert snap.max_age_seconds == 7200
    assert not snap.in_core


def test_the_defaults() -> None:
    (snap,) = parse(ONE).memory.snapshots
    assert snap.args == {}
    assert snap.scope == "shared"
    assert snap.person_id is None
    assert snap.items is None
    assert snap.max_chars == 20000
    assert snap.max_age_seconds == 900  # 3 x 5m is less than 15m


def test_max_age_defaults_to_three_times_every() -> None:
    snap = Snapshot(name="a", server="tasks", tool="t", every="1h")
    assert snap.max_age_seconds == 3 * 3600


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("server: nowhere", "unknown server"),
        ("scope: sam", "unknown person id"),
        ("scope: mia\n  server: mine", "not allowed on 'mine'"),
        ("server: mine", "the scope must be a person"),
        ("every: 30s", "from 1m to 24h"),
        ("every: 25h", "from 1m to 24h"),
        ("every: often", "duration"),
        ("max_age: 0m", "more than zero"),
        ("max_chars: 0", "greater than 0"),
        ("name: Bad_Name", "snapshot name"),
        ('tool: ""', "must not be empty"),
        ("color: red", "Extra inputs"),
    ],
)
def test_a_bad_entry_is_rejected(entry: str, message: str) -> None:
    lines = {
        "name": "open-tasks",
        "server": "tasks",
        "tool": "list_tasks",
        "every": "5m",
    }
    overrides = [line.strip() for line in entry.split("\n")]
    for line in overrides:
        key, _, value = line.partition(":")
        lines[key.strip()] = value.strip()
    body = "- " + "\n  ".join(f"{k}: {v}" for k, v in lines.items())
    with pytest.raises(config.ConfigError, match=message):
        parse(body)


def test_a_duplicate_name_is_rejected() -> None:
    with pytest.raises(config.ConfigError, match="duplicate snapshot name"):
        parse(ONE + ONE)


def test_the_scheduling_listing_tool_validates() -> None:
    (snap,) = parse(
        """
        - name: own-tasks
          server: scheduling
          tool: list_tasks
          every: 10m
          scope: mia
        """
    ).memory.snapshots
    assert snap.in_core


@pytest.mark.parametrize("tool", ["schedule_task", "cancel_task", "update_task", "pause_task"])
def test_a_scheduling_tool_that_is_not_a_listing_fails(tool: str) -> None:
    with pytest.raises(config.ConfigError, match="allows only"):
        parse(f"- name: own-tasks\n  server: scheduling\n  tool: {tool}\n  every: 10m\n")


@pytest.mark.parametrize(
    ("text", "seconds"), [("30s", 30), ("5m", 300), ("2h", 7200), ("1d", 86400)]
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


def test_parse_duration_refuses_other_text() -> None:
    with pytest.raises(ValueError):
        parse_duration("5 minutes")


def test_fields_is_a_list_of_key_names() -> None:
    from joshua_shared.config import Snapshot

    snap = Snapshot(
        name="low-items", server="tasks", tool="list", every="5m", fields=[" name ", "status"]
    )
    assert snap.fields == ["name", "status"]
    assert (
        Snapshot(name="low-items", server="tasks", tool="list", every="5m", fields="").fields
        is None
    )


def test_fields_rejects_a_value_that_is_not_a_list_of_names() -> None:
    import pytest
    from joshua_shared.config import Snapshot
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Snapshot(name="low-items", server="tasks", tool="list", every="5m", fields="name")


def test_a_jobs_key_is_refused() -> None:
    with pytest.raises(config.ConfigError):
        parse(ONE + "  jobs: true\n")
