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


def test_curator_defaults() -> None:
    curator = config.Jobs().curator
    assert curator.enabled is False
    assert curator.model is None
    assert curator.max_worker_calls == 40
    assert curator.duplicate_sim == 0.92
    assert curator.overlap_sim == 0.70
    assert curator.apply == []
    assert curator.trash is True


def test_curator_overrides_load() -> None:
    text = BASE + (
        "jobs:\n"
        "  curator:\n"
        "    enabled: true\n"
        "    model: claude-haiku-4-5\n"
        "    max_worker_calls: 5\n"
        "    duplicate_sim: 0.95\n"
        "    overlap_sim: 0.8\n"
        "    apply:\n"
        "      - move\n"
        "      - refresh\n"
        "      - move\n"
        "    trash: false\n"
    )
    curator = config.parse(text, {}, source="test.yaml").jobs.curator
    assert curator.enabled is True
    assert curator.model == "claude-haiku-4-5"
    assert curator.max_worker_calls == 5
    assert curator.duplicate_sim == 0.95
    assert curator.overlap_sim == 0.8
    assert curator.apply == ["move", "refresh"]
    assert curator.trash is False


@pytest.mark.parametrize(
    "fragment",
    [
        "  curator:\n    apply:\n      - delete\n",
        "  curator:\n    max_worker_calls: -1\n",
        "  curator:\n    duplicate_sim: 1.5\n",
        "  curator:\n    duplicate_sim: 0.6\n    overlap_sim: 0.7\n",
    ],
)
def test_curator_bad_values_fail(fragment: str) -> None:
    with pytest.raises(config.ConfigError):
        config.parse(BASE + "jobs:\n" + fragment, {}, source="test.yaml")
