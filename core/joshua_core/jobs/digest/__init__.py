"""The digest job: one journal entry for a run of near-duplicate entries.

`groups` finds the candidates and their groups, `worker` holds the `digest`
worker, `prompts` its prompt, `page` the digest entry and the `digest:` line
on each covered entry, and `job` the run itself. See the digest section of
`docs/jobs.md`.
"""

from __future__ import annotations

from joshua_core.jobs.digest.groups import (
    Entry,
    Group,
    build_groups,
    find_candidates,
    is_covered,
    is_digest_entry,
    slug_stem,
)
from joshua_core.jobs.digest.job import DigestJob
from joshua_core.jobs.digest.page import mark, render_digest, unmark
from joshua_core.jobs.digest.worker import DigestAnswer, write_digest

__all__ = [
    "DigestAnswer",
    "DigestJob",
    "Entry",
    "Group",
    "build_groups",
    "find_candidates",
    "is_covered",
    "is_digest_entry",
    "mark",
    "render_digest",
    "slug_stem",
    "unmark",
    "write_digest",
]
