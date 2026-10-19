"""A curator finding: what one check or one worker found about the wiki.

A finding has a `kind`, a `tier`, the wiki paths it is about, a `detail` for
the report, and a `patch`: the action that fixes it, or None when only a
person can. The tier says who applies it:

- **A**: a run applies it, always.
- **B**: a run applies it only when `jobs.curator.apply`, or the `apply`
  argument of the run, names its action.
- **C**: a run records it and never applies it.

The fingerprint is the SHA-256 of the kind, the sorted paths, and the
normalized `key` of the finding. It keeps one finding one row of
`curator_finding` across runs, so a dismissed finding stays dismissed.

A pair of pages that `compare` read is one row of `curator_pair`: the
fingerprint of the pair, the body hash of each page, and when. A run does not
send the pair to `compare` again while both hashes are the same.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

TIER_A = "A"
TIER_B = "B"
TIER_C = "C"

# The kinds that a worker raises. A page that a worker reviews again replaces
# its earlier findings of these kinds.
WORKER_KINDS = ("misplaced", "no-section", "duplicate", "conflict", "refresh")
# The kinds that each worker raises.
KINDS_BY_WORKER: dict[str, tuple[str, ...]] = {
    "place": ("misplaced", "no-section"),
    "compare": ("duplicate", "conflict"),
    "refresh": ("refresh",),
}


@dataclass
class Finding:
    """One finding. `id` and `status` come from the row once it is recorded."""

    kind: str
    tier: str
    paths: list[str]
    key: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    patch: dict[str, Any] | None = None
    id: int | None = None
    status: str = "open"

    @property
    def action(self) -> str | None:
        """The name of the patch action, or None when there is no patch."""
        return self.patch.get("action") if self.patch else None

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.kind, self.paths, self.key)


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def fingerprint(kind: str, paths: list[str], key: str | Any) -> str:
    """The fingerprint of a finding. A `key` that is not a string is JSON first."""
    text = key if isinstance(key, str) else json.dumps(key, sort_keys=True, ensure_ascii=False)
    blob = "\n".join([kind, *sorted(paths), _normalize(text)])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def pair_fingerprint(a: str, b: str) -> str:
    """The fingerprint of a pair of pages for `compare`, in either order."""
    return fingerprint("compare-pair", [a, b], "")


class FindingStore(Protocol):
    """The `curator_finding` and `curator_pair` methods of `Repo`."""

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
    ) -> dict[str, Any]: ...

    async def list_findings(
        self, status: str | None = "open", limit: int = 20
    ) -> list[dict[str, Any]]: ...

    async def get_finding(self, finding_id: int) -> dict[str, Any] | None: ...

    async def set_finding_status(
        self,
        finding_id: int,
        status: str,
        *,
        at: datetime,
        resolved_by: str | None = None,
        note: str | None = None,
    ) -> bool: ...

    async def get_curator_pairs(self, fingerprints: list[str]) -> dict[str, dict[str, Any]]: ...

    async def upsert_curator_pair(
        self, *, fingerprint: str, hash_a: str, hash_b: str, checked_at: datetime
    ) -> None: ...


def from_row(row: dict[str, Any]) -> Finding:
    """A `Finding` from a `curator_finding` row."""
    return Finding(
        kind=row["kind"],
        tier=row["tier"],
        paths=list(row.get("paths") or []),
        detail=dict(row.get("detail") or {}),
        patch=dict(row["patch"]) if row.get("patch") else None,
        id=int(row["id"]),
        status=row.get("status") or "open",
    )
