"""The model of each worker of each job, from `joshua.yaml`.

Each worker takes the first model that is set in its chain:

- research `raise`, `apply`, `editor`: `jobs.research.models.<worker>`,
  `jobs.research.model`, `jobs.model`, `core.reflection_model`.
- research `lookup` (the researcher and the challenger):
  `jobs.research.models.lookup`, `jobs.research.model`, `jobs.model`,
  `internet.model`.
- curator `place`, `compare`, `refresh`: `jobs.curator.models.<worker>`,
  `jobs.curator.model`, `jobs.model`, `core.reflection_model`.
- insights `insight`: `jobs.insights.model`, `jobs.model`,
  `core.reflection_model`.
- digest `digest`: `jobs.digest.model`, `jobs.model`,
  `core.reflection_model`.

`docs/jobs.md` has the same table.
"""

from __future__ import annotations

from joshua_shared.config import JoshuaConfig

RESEARCH_WORKERS = ("raise", "apply", "editor", "lookup")
CURATOR_WORKERS = ("place", "compare", "refresh")
LOOKUP = "lookup"


def research_model(settings: JoshuaConfig, worker: str | None = None) -> str | None:
    """The model of the research worker `worker`. With `worker` None, the
    model of the job: `jobs.research.model`, `jobs.model`,
    `core.reflection_model`. Raises `ValueError` for an unknown worker."""
    jobs = settings.jobs
    if worker is None:
        return jobs.research.model or jobs.model or settings.core.reflection_model
    if worker not in RESEARCH_WORKERS:
        raise ValueError(f"not a research worker: {worker!r}")
    models = jobs.research.models
    own = models.raise_ if worker == "raise" else getattr(models, worker)
    last = settings.internet.model if worker == LOOKUP else settings.core.reflection_model
    return own or jobs.research.model or jobs.model or last


def curator_model(settings: JoshuaConfig, worker: str | None = None) -> str | None:
    """The model of the curator worker `worker`. With `worker` None, the
    model of the job: `jobs.curator.model`, `jobs.model`,
    `core.reflection_model`. Raises `ValueError` for an unknown worker."""
    jobs = settings.jobs
    own: str | None = None
    if worker is not None:
        if worker not in CURATOR_WORKERS:
            raise ValueError(f"not a curator worker: {worker!r}")
        own = getattr(jobs.curator.models, worker)
    return own or jobs.curator.model or jobs.model or settings.core.reflection_model


def insights_model(settings: JoshuaConfig) -> str | None:
    """The model of the insight worker: `jobs.insights.model`, `jobs.model`,
    `core.reflection_model`."""
    jobs = settings.jobs
    return jobs.insights.model or jobs.model or settings.core.reflection_model


def digest_model(settings: JoshuaConfig) -> str | None:
    """The model of the digest worker: `jobs.digest.model`, `jobs.model`,
    `core.reflection_model`."""
    jobs = settings.jobs
    return jobs.digest.model or jobs.model or settings.core.reflection_model


__all__ = [
    "CURATOR_WORKERS",
    "LOOKUP",
    "RESEARCH_WORKERS",
    "curator_model",
    "digest_model",
    "insights_model",
    "research_model",
]
