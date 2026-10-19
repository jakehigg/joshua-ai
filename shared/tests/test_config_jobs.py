"""The `jobs:` section of joshua.yaml: its defaults, its overrides, and its
bounds."""

from __future__ import annotations

from pathlib import Path

import pytest
from joshua_shared import config

ROOT = Path(__file__).resolve().parents[2]

BASE = """
name: Home
timezone: America/New_York
people:
  - id: alex
    name: Alex
"""


def test_jobs_defaults_are_off_and_documented_values() -> None:
    cfg = config.parse(BASE, {}, source="test.yaml")
    jobs = cfg.jobs
    assert jobs.enabled is False
    assert jobs.model is None
    assert jobs.bundle.sim == pytest.approx(0.70)
    assert jobs.bundle.max_chars == 24000
    assert jobs.bundle.recent_days == 14
    assert jobs.topics.exclude == ["vocabulary.md", "*/README.md"]
    assert jobs.curator.enabled is False
    assert jobs.research.enabled is False
    assert jobs.insights.enabled is False


def test_jobs_overrides_load() -> None:
    text = BASE + (
        "jobs:\n"
        "  enabled: true\n"
        "  model: claude-haiku-4-5\n"
        "  bundle:\n"
        "    sim: 0.8\n"
        "    max_chars: 1000\n"
        "    recent_days: 30\n"
        "  topics:\n"
        "    exclude:\n"
        "      - drafts/*\n"
    )
    cfg = config.parse(text, {}, source="test.yaml")
    assert cfg.jobs.enabled is True
    assert cfg.jobs.model == "claude-haiku-4-5"
    assert cfg.jobs.bundle.sim == pytest.approx(0.8)
    assert cfg.jobs.bundle.max_chars == 1000
    assert cfg.jobs.bundle.recent_days == 30
    assert cfg.jobs.topics.exclude == ["drafts/*"]


@pytest.mark.parametrize(
    "fragment",
    [
        "  bundle:\n    sim: 1.5\n",
        "  bundle:\n    max_chars: 0\n",
        "  bundle:\n    recent_days: 0\n",
        "  bogus: 1\n",
        "  curator:\n    bogus: 1\n",
    ],
)
def test_jobs_bad_values_fail(fragment: str) -> None:
    with pytest.raises(config.ConfigError):
        config.parse(BASE + "jobs:\n" + fragment, {}, source="test.yaml")


def test_example_file_jobs_section_matches_the_defaults() -> None:
    text = (ROOT / "joshua.example.yaml").read_text()
    cfg = config.parse(text, {}, source="joshua.example.yaml")
    assert cfg.jobs == config.Jobs()
