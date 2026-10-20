"""`jobs/models.py`: the model of each job worker, each level of each chain,
and the model that a curator run gives to each worker and to its usage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from curator_fakes import FakeCuratorStore, FakeFindingStore, FakeWorkers, build_curator_wiki
from jobs_fakes import fixed_clock, make_settings
from joshua_core.jobs.curator.job import CuratorJob
from joshua_core.jobs.curator.prompts import COMPARE_PROMPT, PLACE_PROMPT, REFRESH_PROMPT
from joshua_core.jobs.models import (
    CURATOR_WORKERS,
    RESEARCH_WORKERS,
    curator_model,
    insights_model,
    research_model,
)
from joshua_core.memory import embed as embed_module
from joshua_shared.config import ConfigError

CORE = "core:\n  reflection_model: m-reflect\n"
INTERNET = "internet:\n  model: m-internet\n"


def _settings(jobs: str = "") -> Any:
    return make_settings(CORE + INTERNET + "jobs:\n  enabled: true\n" + jobs)


RESEARCH_ALL = (
    "  model: m-jobs\n"
    "  research:\n"
    "    model: m-research\n"
    "    models:\n"
    "      raise: m-raise\n"
    "      apply: m-apply\n"
    "      editor: m-editor\n"
    "      lookup: m-lookup\n"
)


@pytest.mark.parametrize("worker", ["raise", "apply", "editor"])
def test_a_tool_less_research_worker_takes_each_level_in_turn(worker: str) -> None:
    assert research_model(_settings(), worker) == "m-reflect"
    assert research_model(_settings("  model: m-jobs\n"), worker) == "m-jobs"
    job = "  model: m-jobs\n  research:\n    model: m-research\n"
    assert research_model(_settings(job), worker) == "m-research"
    assert research_model(_settings(RESEARCH_ALL), worker) == f"m-{worker}"


def test_the_lookup_chain_ends_at_the_internet_model() -> None:
    assert research_model(_settings(), "lookup") == "m-internet"
    assert research_model(_settings("  model: m-jobs\n"), "lookup") == "m-jobs"
    job = "  research:\n    model: m-research\n"
    assert research_model(_settings(job), "lookup") == "m-research"
    assert research_model(_settings(RESEARCH_ALL), "lookup") == "m-lookup"


def test_one_worker_knob_leaves_the_other_workers_alone() -> None:
    settings = _settings("  research:\n    models:\n      editor: m-editor\n")
    assert research_model(settings, "editor") == "m-editor"
    assert research_model(settings, "raise") == "m-reflect"
    assert research_model(settings, "apply") == "m-reflect"
    assert research_model(settings, "lookup") == "m-internet"


def test_the_research_job_model_with_no_worker() -> None:
    assert research_model(_settings()) == "m-reflect"
    assert research_model(_settings(RESEARCH_ALL)) == "m-research"


@pytest.mark.parametrize("worker", CURATOR_WORKERS)
def test_a_curator_worker_takes_each_level_in_turn(worker: str) -> None:
    assert curator_model(_settings(), worker) == "m-reflect"
    assert curator_model(_settings("  model: m-jobs\n"), worker) == "m-jobs"
    job = "  model: m-jobs\n  curator:\n    model: m-curator\n"
    assert curator_model(_settings(job), worker) == "m-curator"
    own = job + f"    models:\n      {worker}: m-own\n"
    assert curator_model(_settings(own), worker) == "m-own"
    others = [w for w in CURATOR_WORKERS if w != worker]
    assert {curator_model(_settings(own), w) for w in others} == {"m-curator"}
    assert curator_model(_settings(own)) == "m-curator"


def test_the_insights_chain() -> None:
    assert insights_model(_settings()) == "m-reflect"
    assert insights_model(_settings("  model: m-jobs\n")) == "m-jobs"
    job = "  model: m-jobs\n  insights:\n    model: m-insight\n"
    assert insights_model(_settings(job)) == "m-insight"


def test_an_unknown_worker_is_an_error() -> None:
    with pytest.raises(ValueError):
        research_model(_settings(), "place")
    with pytest.raises(ValueError):
        curator_model(_settings(), "editor")
    assert set(RESEARCH_WORKERS) == {"raise", "apply", "editor", "lookup"}


def test_an_unknown_worker_key_in_the_config_is_refused() -> None:
    with pytest.raises((ConfigError, ValueError)):
        _settings("  research:\n    models:\n      writer: m\n")
    with pytest.raises((ConfigError, ValueError)):
        _settings("  curator:\n    models:\n      raise: m\n")


class _ModelWorkers(FakeWorkers):
    """The curator fakes, and the model of each call by worker."""

    def __init__(self) -> None:
        super().__init__()
        self.models: dict[str, set[Any]] = {}

    async def __call__(self, **kwargs: Any) -> dict[str, Any] | None:
        name = {PLACE_PROMPT: "place", COMPARE_PROMPT: "compare", REFRESH_PROMPT: "refresh"}[
            kwargs["system_prompt"]
        ]
        self.models.setdefault(name, set()).add(kwargs.get("model"))
        return await super().__call__(**kwargs)


async def test_a_curator_run_gives_each_worker_its_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(embed_module, "embed", lambda text, model="": [1.0] + [0.0] * 383)
    data = tmp_path / "data"
    build_curator_wiki(data)
    fake = _ModelWorkers()
    settings = _settings(
        "  curator:\n"
        "    enabled: true\n"
        "    model: m-curator\n"
        "    models:\n"
        "      place: m-place\n"
        "      refresh: m-refresh\n"
    )
    job = CuratorJob(
        store=FakeCuratorStore(),
        findings=FakeFindingStore(),
        settings=settings,
        data_dir=data,
        runner=fake,
    )
    report = await job.run_once(args={}, dry_run=True, clock=fixed_clock())
    expected = {"place": "m-place", "compare": "m-curator", "refresh": "m-refresh"}
    assert fake.models, "the run called no worker"
    for worker, models in fake.models.items():
        assert models == {expected[worker]}
    for worker, entry in report.data["usage"].items():
        assert entry["model"] == expected[worker]
