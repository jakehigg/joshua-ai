"""Offline fakes and a fixture wiki for the jobs tests.

`build_wiki` writes a wiki that holds a topic, its links, its vocabulary term,
journal entries that are and are not about it, a dossier, and insights.
`FakeStore` answers the bundle's vector query from a canned map.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from joshua_shared import config as config_module

CONFIG = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
"""

# 2026-10-19 12:00 in New York.
NOW = datetime(2026, 10, 19, 16, 0, tzinfo=UTC)

TOPIC = "houseplants/snake-plant.md"


def make_settings(extra: str = "") -> Any:
    return config_module.parse(CONFIG + extra, env={}, source="<test>")


WIKI = {
    "Home.md": "# Home\n",
    "houseplants.md": "# Houseplants\n",
    TOPIC: (
        "---\naliases: [Sansevieria]\n---\n"
        "# Snake plant\n\n"
        "Gritty mix, watered to runoff. See [pothos](pothos.md), "
        "[[watering-schedule]], [the garden](../garden/irrigation.md), "
        "[a site](https://example.com/plants), [myself](snake-plant.md), "
        "[gone](nope.md), and ![a photo](pic.png).\n"
    ),
    "houseplants/pothos.md": "# Pothos\n\nTrails.\n",
    "houseplants/watering-schedule.md": "# Watering schedule\n\nMonthly.\n",
    "garden/irrigation.md": "# Irrigation\n",
    "vocabulary.md": (
        "# Vocabulary\n\n"
        "- **Snakey**: our [snake plant](houseplants/snake-plant.md) in the hall\n"
        "- [Pothy](houseplants/pothos.md): the pothos\n"
        "- no colon here [snake plant](houseplants/snake-plant.md)\n"
        "- see [docs](https://example.com/x) about [it](houseplants/snake-plant.md)\n"
    ),
    "journal/2026/10/01/water.md": "---\ndate: 2026-10-01\n---\nWatered the snake plants today.\n",
    "journal/2026/10/02/word.md": "The snakeplanter is a new word.\n",
    "journal/2026/10/05/hall.md": "Snakey looks good in the hall.\n",
    "journal/2026/10/07/repot.md": "Repotted the SANSEVIERIA.\n",
    "journal/2026/10/08/bread.md": "Baked bread. Nothing about plants.\n",
    "journal/2026/10/09/pothos.md": "Trimmed the pothos.\n",
    "journal/2026/10/10/bundle-20261010-1.md": (
        "---\nsource: bundle\nrun_id: 1\n---\nThe snake plant bundle.\n"
    ),
    "journal/2026/10/30/future.md": "The snake plant, in the future.\n",
    "knowledge/houseplants/snake-plant.md": (
        "---\ntopic: houseplants/snake-plant.md\n---\nDossier.\n"
    ),
    "knowledge/houseplants/snake-plant/winter-watering.md": "# Winter watering\n",
    "insights/houseplants/too-wet.md": (
        "---\ntopic: houseplants/snake-plant.md\nstatus: new\n---\nToo wet.\n"
    ),
    "insights/houseplants/old.md": (
        "---\ntopic: houseplants/snake-plant.md\nstatus: dismissed\n---\nOld.\n"
    ),
    "insights/garden/dry.md": "---\ntopic: garden/irrigation.md\nstatus: new\n---\nDry.\n",
}


def build_wiki(data_root: Path, pages: dict[str, str] | None = None) -> Path:
    """Write the fixture wiki under `data_root/wiki` and return the wiki root."""
    wiki = data_root / "wiki"
    for rel, text in (pages if pages is not None else WIKI).items():
        path = wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return wiki


class FakeStore:
    """Answers `kb_near_paths` from `near` and records each call."""

    def __init__(self, near: dict[str, float] | None = None) -> None:
        self.near = near or {}
        self.calls: list[dict[str, Any]] = []

    async def kb_near_paths(
        self,
        embeddings: Sequence[list[float]],
        *,
        kinds: Sequence[str],
        min_sim: float,
        source: str = "files",
    ) -> dict[str, float]:
        self.calls.append(
            {"count": len(embeddings), "kinds": list(kinds), "min_sim": min_sim, "source": source}
        )
        return dict(self.near)


class FakeJobRunStore:
    """An in-memory `job_run` table."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.fail_finish = False

    async def create_job_run(
        self, *, job: str, args: dict[str, Any], dry_run: bool, started_at: datetime
    ) -> int:
        run_id = len(self.rows) + 1
        self.rows[run_id] = {
            "id": run_id,
            "job": job,
            "started_at": started_at,
            "finished_at": None,
            "dry_run": dry_run,
            "args": args,
            "commit_sha": None,
            "report": None,
            "calls": 0,
            "tokens": 0,
            "error": None,
        }
        return run_id

    async def finish_job_run(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        report: dict[str, Any],
        commit_sha: str | None,
        calls: int,
        tokens: int,
        error: str | None,
    ) -> None:
        if self.fail_finish:
            raise RuntimeError("database gone")
        self.rows[run_id].update(
            finished_at=finished_at,
            report=report,
            commit_sha=commit_sha,
            calls=calls,
            tokens=tokens,
            error=error,
        )

    async def get_job_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.rows.get(run_id)
        return dict(row) if row is not None else None

    async def list_job_runs(self, job: str | None, limit: int = 20) -> list[dict[str, Any]]:
        rows = [r for r in self.rows.values() if job is None or r["job"] == job]
        return [dict(r) for r in sorted(rows, key=lambda r: r["id"], reverse=True)[:limit]]


def fixed_clock(when: datetime = NOW):
    return lambda: when
