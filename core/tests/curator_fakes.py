"""Offline fakes and a fixture wiki for the curator tests.

`build_curator_wiki` writes a wiki with one of each thing the curator finds.
`FakeCuratorStore` answers the vector queries, `FakeFindingStore` is an
in-memory `curator_finding` table, and `FakeWorkers` answers the three workers
from canned answers and records each call.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from joshua_core.jobs.curator.prompts import COMPARE_PROMPT, PLACE_PROMPT, REFRESH_PROMPT

SNAKE = "houseplants/snake-plant.md"
POTHOS = "houseplants/pothos.md"
TOMATOES = "houseplants/tomatoes.md"
CACTUS = "houseplants/orphan-cactus.md"
ROSES = "garden/roses.md"
ROSE_CARE = "garden/rose-care.md"
KITCHEN_NOTES = "kitchen/notes.md"
MISC_NOTES = "misc/notes.md"
SKILL = "skills/watering.md"
WATER = "journal/2026/10/01/water.md"
HALL = "journal/2026/10/05/hall.md"
REPORT = "journal/2026/10/10/curator-20261010-1.md"
DOT_STRAY = "houseplants/._snake-plant.md"
EMPTY = "empty.md"

CURATOR_WIKI: dict[str, str] = {
    "Home.md": (
        "# Home\n\n"
        "## Sections\n\n"
        "- [Houseplants](houseplants.md): the plants that live inside\n"
        "- [Garden](/wiki/garden.md) — the yard and the beds\n"
        "- [Kitchen](kitchen.md): food and recipes\n"
    ),
    "houseplants.md": (
        "# Houseplants\n\n"
        "The plants that live in the house.\n\n"
        "- [Snake plant](houseplants/snake-plant.md)\n"
        "- [Pothos](houseplants/pothos.md)\n"
        "- [Tomatoes](houseplants/tomatoes.md)\n"
        "- [Fern](houseplants/fern.md)\n"
    ),
    SNAKE: (
        "---\naliases: [Snakey]\n---\n"
        "# Snake plant\n\n"
        "Watered every 2 weeks in the hall. It shares the hall with the "
        "[pothos](pothos.md) and the [tomatoes](tomatoes.md).\n\n"
        "The light is the light of the hall, and the pot is the pot.\n"
    ),
    POTHOS: "# Pothos\n\nTrails over the shelf. Not like the [roses](roses.md). [[snake-plant]]\n",
    TOMATOES: "# Tomatoes\n\nThey grow in the south bed, next to the [pothos](pothos.md).\n",
    CACTUS: "# Cactus\n\nA small cactus on the sill.\n",
    "garden.md": (
        "# Garden\n\n"
        "The yard, the beds, and what grows outside.\n\n"
        "- [Roses](garden/roses.md)\n"
        "- [Rose care](garden/rose-care.md)\n"
    ),
    ROSES: "# Roses\n\nPrune in March. See [the notes](notes.md).\n",
    ROSE_CARE: "# Rose care\n\nPrune the roses in March.\n",
    "kitchen.md": "# Kitchen\n\nFood and recipes.\n\n- [Notes](kitchen/notes.md)\n",
    KITCHEN_NOTES: "# Kitchen notes\n\nSalt the water.\n",
    MISC_NOTES: "# Misc notes\n\nOdds and ends.\n",
    SKILL: (
        "---\nname: watering\ntriggers:\n  - water\n---\n"
        "# Watering\n\nSee [the plant](../houseplants/snake-plant.md) and `[code](nope.md)`.\n"
    ),
    WATER: "Watered the snake plant. It gets water every 4 weeks now.\n",
    HALL: (
        "---\ndate: 2026-10-05\npeople: []\n---\n"
        "Moved the snake plant to the bedroom. See [the plan](gone-plan.md).\n"
    ),
    REPORT: (
        "---\ndate: 2026-10-10\nsource: curator\nrun_id: 1\n---\n"
        "# curator run\n\nThe snake plant [page](missing-page.md).\n"
    ),
    "people/alex.md": "# Alex\n\nLikes [ferns](fern.md). Has [[nobody]].\n",
    "joshua-docs/jobs.md": "# Jobs\n\nSee [the guide](nowhere.md).\n",
    DOT_STRAY: "resource fork\n",
    EMPTY: "",
}

# The topic pages, each with its own direction, except the two rose pages.
VECTORS: dict[str, list[float]] = {
    f"wiki/{SNAKE}": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    f"wiki/{POTHOS}": [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    f"wiki/{TOMATOES}": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
    f"wiki/{CACTUS}": [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
    f"wiki/{ROSES}": [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0],
    f"wiki/{ROSE_CARE}": [0.0, 0.0, 0.0, 0.0, 0.99, 0.05, 0.0],
    f"wiki/{KITCHEN_NOTES}": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
    f"wiki/{MISC_NOTES}": [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0],
}


def build_curator_wiki(data_root: Path, pages: dict[str, str] | None = None) -> Path:
    wiki = data_root / "wiki"
    for rel, text in (pages if pages is not None else CURATOR_WIKI).items():
        path = wiki / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return wiki


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file under `root`, dot paths too, except `.git`."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not rel.startswith(".git/") and rel != ".git":
            out[rel] = path.read_bytes()
    return out


def changed(before: dict[str, bytes], after: dict[str, bytes]) -> set[str]:
    keys = set(before) | set(after)
    return {k for k in keys if before.get(k) != after.get(k)}


class FakeCuratorStore:
    """Answers `kb_near_paths` with nothing and `kb_mean_vectors` from `vectors`."""

    def __init__(self, vectors: dict[str, list[float]] | None = None, fail: bool = False):
        self.vectors = dict(VECTORS if vectors is None else vectors)
        self.fail = fail
        self.mean_calls = 0

    async def kb_near_paths(
        self,
        embeddings: Sequence[list[float]],
        *,
        kinds: Sequence[str],
        min_sim: float,
        source: str = "files",
    ) -> dict[str, float]:
        return {}

    async def kb_mean_vectors(
        self, paths: Sequence[str], *, source: str = "files"
    ) -> dict[str, list[float]]:
        self.mean_calls += 1
        if self.fail:
            raise RuntimeError("no database")
        return {p: self.vectors[p] for p in paths if p in self.vectors}


class FakeFindingStore:
    """An in-memory `curator_finding` table with the semantics of `Repo`."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.upserts: list[dict[str, Any]] = []
        self.people: dict[str, Any] = {}
        self.pairs: dict[str, dict[str, Any]] = {}

    async def upsert_finding(
        self,
        *,
        run_id: int | None,
        kind: str,
        tier: str,
        fingerprint: str,
        paths: list[str],
        detail: dict[str, Any],
        patch: dict[str, Any] | None,
        status: str,
        seen_at: datetime,
    ) -> dict[str, Any]:
        self.upserts.append({"fingerprint": fingerprint, "kind": kind, "status": status})
        for row in self.rows.values():
            if row["fingerprint"] == fingerprint:
                kept = {"note": row["detail"]["note"]} if "note" in row["detail"] else {}
                row.update(
                    run_id=run_id,
                    tier=tier,
                    detail={**detail, **kept},
                    patch=patch,
                    last_seen=seen_at,
                )
                if row["status"] == "stale":
                    row.update(status="open", resolved_at=None)
                return dict(row)
        rid = len(self.rows) + 1
        resolved = status in ("applied", "dismissed", "stale")
        self.rows[rid] = {
            "id": rid,
            "run_id": run_id,
            "kind": kind,
            "tier": tier,
            "fingerprint": fingerprint,
            "paths": list(paths),
            "detail": dict(detail),
            "patch": patch,
            "status": status,
            "first_seen": seen_at,
            "last_seen": seen_at,
            "resolved_at": seen_at if resolved else None,
            "resolved_by": "curator" if resolved else None,
        }
        return dict(self.rows[rid])

    async def list_findings(
        self, status: str | None = "open", limit: int = 20
    ) -> list[dict[str, Any]]:
        rows = [r for r in self.rows.values() if status is None or r["status"] == status]
        return [dict(r) for r in sorted(rows, key=lambda r: r["id"], reverse=True)[:limit]]

    async def get_finding(self, finding_id: int) -> dict[str, Any] | None:
        row = self.rows.get(finding_id)
        return dict(row) if row else None

    async def set_finding_status(
        self,
        finding_id: int,
        status: str,
        *,
        at: datetime,
        resolved_by: str | None = None,
        note: str | None = None,
    ) -> bool:
        row = self.rows.get(finding_id)
        if row is None:
            return False
        resolved = status in ("applied", "dismissed", "stale")
        row.update(
            status=status,
            resolved_at=at if resolved else None,
            resolved_by=resolved_by if resolved else None,
        )
        if note is not None:
            row["detail"] = {**row["detail"], "note": note}
        return True

    async def get_curator_pairs(self, fingerprints: list[str]) -> dict[str, dict[str, Any]]:
        return {f: dict(self.pairs[f]) for f in fingerprints if f in self.pairs}

    async def upsert_curator_pair(
        self, *, fingerprint: str, hash_a: str, hash_b: str, checked_at: datetime
    ) -> None:
        self.pairs[fingerprint] = {
            "fingerprint": fingerprint,
            "hash_a": hash_a,
            "hash_b": hash_b,
            "checked_at": checked_at,
        }

    async def get_person(self, pid: str) -> Any:
        return self.people.get(pid)

    def by_kind(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.rows.values() if r["kind"] == kind]


_PATH_RE = re.compile(r'<(?:page|page_a|page_b) path="([^"]+)">')


class FakeWorkers:
    """A worker runner. It reads the worker from the system prompt and the page
    from the user text, and answers from `place`, `compare`, and `refresh`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.place: dict[str, Any] = {
            SNAKE: _place("Houseplants", "houseplants"),
            POTHOS: _place("Houseplants", "houseplants"),
            CACTUS: _place("Houseplants", "houseplants"),
            TOMATOES: _place("Garden", "garden", "They grow outside, in a bed."),
            ROSES: _place("Garden", "garden"),
            ROSE_CARE: _place("Garden", "garden"),
            KITCHEN_NOTES: _place("Kitchen", "kitchen"),
            MISC_NOTES: _place("none", "misc", "It fits no section."),
        }
        self.compare: dict[tuple[str, str], Any] = {
            (ROSE_CARE, ROSES): {
                "relation": "duplicate",
                "claims": [],
                "canonical": "b",
                "confidence": "high",
            }
        }
        self.refresh: dict[str, Any] = {
            SNAKE: {
                "status": "stale",
                "patch": [
                    {
                        "find": "every 2 weeks",
                        "replace": "every 4 weeks",
                        "because": f"{WATER} says every 4 weeks",
                    }
                ],
                "confidence": "high",
            }
        }

    @property
    def count(self) -> int:
        return len(self.calls)

    def names(self, worker: str) -> list[list[str]]:
        return [paths for name, paths in self.calls if name == worker]

    async def __call__(self, **kwargs: Any) -> dict[str, Any] | None:
        prompt = kwargs["system_prompt"]
        paths = _PATH_RE.findall(kwargs["user_text"])
        if prompt == PLACE_PROMPT:
            self.calls.append(("place", paths))
            return self.place.get(paths[0])
        if prompt == COMPARE_PROMPT:
            self.calls.append(("compare", paths))
            return self.compare.get((paths[0], paths[1]))
        if prompt == REFRESH_PROMPT:
            self.calls.append(("refresh", paths))
            return self.refresh.get(paths[0], {"status": "current", "confidence": "high"})
        raise AssertionError("unknown worker")


def _place(section: str, folder: str, reason: str = "It fits.") -> dict[str, Any]:
    return {"section": section, "folder": folder, "confidence": "high", "reason": reason}
