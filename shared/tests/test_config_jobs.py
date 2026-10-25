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
    assert jobs.bundle.sim == pytest.approx(0.78)
    assert jobs.bundle.max_vector_entries == 6
    assert jobs.bundle.exclude_slugs == []
    assert jobs.bundle.max_chars == 24000
    assert jobs.bundle.recent_days == 14
    assert jobs.topics.exclude == ["vocabulary.md", "*/README.md", "coach/profiles/*"]
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
        "    max_vector_entries: 2\n"
        "    exclude_slugs:\n"
        "      - garden-weather-log-*\n"
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
    assert cfg.jobs.bundle.max_vector_entries == 2
    assert cfg.jobs.bundle.exclude_slugs == ["garden-weather-log-*"]
    assert cfg.jobs.bundle.max_chars == 1000
    assert cfg.jobs.bundle.recent_days == 30
    assert cfg.jobs.topics.exclude == ["drafts/*"]


def test_research_person_sections_load_and_normalize() -> None:
    text = BASE + (
        "jobs:\n  research:\n    person_sections:\n      - ' health '\n      - health\n      - ''\n"
    )
    research = config.parse(text, {}, source="test.yaml").jobs.research
    assert research.person_sections == ["health"]


@pytest.mark.parametrize(
    "fragment",
    [
        "  bundle:\n    sim: 1.5\n",
        "  bundle:\n    max_chars: 0\n",
        "  bundle:\n    max_vector_entries: -1\n",
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


def test_research_defaults() -> None:
    research = config.Jobs().research
    assert research.enabled is False
    assert research.model is None
    assert research.topics_per_run == 1
    assert research.questions_per_topic == 8
    assert research.questions_per_run == 3
    assert research.coverage_target == 0.6
    assert research.dup_sim == 0.85
    assert research.record_sim == 0.80
    assert research.max_holds == 2
    assert research.refresh_days == 180
    assert research.lookup_timeout_seconds == 300
    assert research.lookup_max_turns == 14
    assert research.lookup_max_age_days == 7
    assert research.editor_timeout_seconds == 240
    assert research.trusted_domains == [".edu", ".gov", ".ac.uk", "extension.", ".int"]
    assert "reddit.com" in research.distrusted_domains
    assert len(research.distrusted_domains) == 7
    assert research.person_sections == ["coach", "people"]


def test_research_overrides_load() -> None:
    text = BASE + (
        "jobs:\n"
        "  research:\n"
        "    enabled: true\n"
        "    model: claude-haiku-4-5\n"
        "    questions_per_topic: 4\n"
        "    questions_per_run: 1\n"
        "    coverage_target: 0.8\n"
        "    dup_sim: 0.9\n"
        "    record_sim: 0.7\n"
        "    max_holds: 3\n"
        "    refresh_days: 30\n"
        "    lookup_timeout_seconds: 600\n"
        "    lookup_max_turns: 30\n"
        "    lookup_max_age_days: 30\n"
        "    editor_timeout_seconds: 90\n"
        "    trusted_domains:\n"
        "      - ' .EDU '\n"
        "      - .edu\n"
        "    distrusted_domains:\n"
        "      - Example.COM\n"
    )
    research = config.parse(text, {}, source="test.yaml").jobs.research
    assert research.enabled is True
    assert research.model == "claude-haiku-4-5"
    assert research.questions_per_topic == 4
    assert research.questions_per_run == 1
    assert research.coverage_target == 0.8
    assert research.dup_sim == 0.9
    assert research.record_sim == 0.7
    assert research.max_holds == 3
    assert research.refresh_days == 30
    assert research.lookup_timeout_seconds == 600
    assert research.lookup_max_turns == 30
    assert research.lookup_max_age_days == 30
    assert research.editor_timeout_seconds == 90
    assert research.trusted_domains == [".edu"]
    assert research.distrusted_domains == ["example.com"]


@pytest.mark.parametrize(
    "fragment",
    [
        "  research:\n    topics_per_run: 2\n",
        "  research:\n    questions_per_run: 0\n",
        "  research:\n    dup_sim: 1.5\n",
        "  research:\n    record_sim: -0.1\n",
        "  research:\n    max_holds: 0\n",
        "  research:\n    lookup_timeout_seconds: 0\n",
        "  research:\n    lookup_max_turns: 0\n",
        "  research:\n    lookup_max_age_days: 0\n",
        "  research:\n    editor_timeout_seconds: 0\n",
        "  research:\n    trusted_domains:\n      - example.com/path\n",
        "  research:\n    person_sections:\n      - coach/profiles\n",
        "  research:\n    person_sections:\n      - .hidden\n",
        "  research:\n    exclude:\n      - x.md\n",
    ],
)
def test_research_bad_values_fail(fragment: str) -> None:
    with pytest.raises(config.ConfigError):
        config.parse(BASE + "jobs:\n" + fragment, {}, source="test.yaml")


def test_insights_defaults() -> None:
    insights = config.Jobs().insights
    assert insights.enabled is False
    assert insights.model is None
    assert insights.max_per_run == 5
    assert insights.dup_sim == 0.88
    assert insights.default_valid_days == 60
    assert insights.max_valid_days == 45
    assert insights.examples == 12
    assert insights.max_open == 5
    assert insights.after_research is True


def test_insights_overrides_load() -> None:
    text = BASE + (
        "jobs:\n"
        "  insights:\n"
        "    enabled: true\n"
        "    model: claude-haiku-4-5\n"
        "    max_per_run: 2\n"
        "    dup_sim: 0.9\n"
        "    default_valid_days: 30\n"
        "    max_valid_days: 20\n"
        "    examples: 0\n"
        "    max_open: 8\n"
        "    after_research: false\n"
    )
    insights = config.parse(text, {}, source="test.yaml").jobs.insights
    assert insights.enabled is True
    assert insights.model == "claude-haiku-4-5"
    assert insights.max_per_run == 2
    assert insights.dup_sim == 0.9
    assert insights.default_valid_days == 30
    assert (insights.max_valid_days, insights.examples, insights.max_open) == (20, 0, 8)
    assert insights.after_research is False


@pytest.mark.parametrize(
    "fragment",
    [
        "  insights:\n    max_per_run: 0\n",
        "  insights:\n    dup_sim: 1.5\n",
        "  insights:\n    default_valid_days: 0\n",
        "  insights:\n    max_valid_days: 0\n",
        "  insights:\n    examples: -1\n",
        "  insights:\n    max_open: 0\n",
        "  insights:\n    bogus: 1\n",
    ],
)
def test_insights_bad_values_fail(fragment: str) -> None:
    with pytest.raises(config.ConfigError):
        config.parse(BASE + "jobs:\n" + fragment, {}, source="test.yaml")


def test_the_open_question_budget_defaults_and_bounds() -> None:
    research = config.Jobs().research
    assert (research.max_open_questions, research.max_open_questions_per_topic) == (10, 2)
    text = BASE + (
        "jobs:\n  research:\n    max_open_questions: 4\n    max_open_questions_per_topic: 1\n"
    )
    loaded = config.parse(text, {}, source="test.yaml").jobs.research
    assert (loaded.max_open_questions, loaded.max_open_questions_per_topic) == (4, 1)
    for bad in ("max_open_questions: -1", "max_open_questions_per_topic: 51"):
        with pytest.raises(config.ConfigError):
            config.parse(BASE + f"jobs:\n  research:\n    {bad}\n", {}, source="test.yaml")
