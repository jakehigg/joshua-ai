"""The tracking keys: how the curator knows that it has reviewed a page.

A page the curator reviewed carries two frontmatter keys:

```yaml
curated: 2026-10-19       # the local day of the run that reviewed it
curated_hash: 7f3a9c1e04b2  # the body hash at that review
```

The body hash is the first 12 hex characters of the SHA-256 of the body
(the text after the frontmatter block), with trailing whitespace removed. A
change to the frontmatter alone does not change it. A page whose body hash
equals `curated_hash` is not sent to a worker again.

The keys go on a `wiki/**.md` page only. Never on a page under `people/` or
`joshua-docs/`, never on a dot path, and never on a journal entry that a job
run wrote (its frontmatter has `run_id`).
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import PurePosixPath
from typing import Any

from joshua_shared import frontmatter

CURATED_KEY = "curated"
HASH_KEY = "curated_hash"
HASH_CHARS = 12
# The top-level folders the curator never writes.
UNTOUCHED_FOLDERS = ("people", "joshua-docs")


def body_hash(text: str) -> str:
    """The hash of the body of `text`. See the module docstring."""
    _, body = frontmatter.split(text)
    return hashlib.sha256(body.rstrip().encode("utf-8")).hexdigest()[:HASH_CHARS]


def needs_review(text: str) -> bool:
    """True when the page has no `curated_hash`, or its body changed since."""
    meta, _ = frontmatter.split(text)
    recorded = meta.get(HASH_KEY)
    return not isinstance(recorded, str) or recorded != body_hash(text)


def curated_day(text: str) -> date | None:
    """The `curated` day of a page, or None when it has none."""
    meta, _ = frontmatter.split(text)
    value = meta.get(CURATED_KEY)
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def is_job_report(meta: dict[str, Any]) -> bool:
    """True for a journal entry that a job run wrote."""
    return "run_id" in meta


def is_untouched(rel: str) -> bool:
    """True for a wiki path the curator never writes: `people/`, `joshua-docs/`,
    or a path with a part that starts with a dot."""
    parts = PurePosixPath(rel).parts
    if not parts:
        return True
    if any(part.startswith(".") for part in parts):
        return True
    return len(parts) > 1 and parts[0] in UNTOUCHED_FOLDERS


def may_track(rel: str, text: str) -> bool:
    """True when the curator may write the tracking keys on this page."""
    if not rel.endswith(".md") or is_untouched(rel):
        return False
    meta, _ = frontmatter.split(text)
    return not is_job_report(meta)


def with_tracking(text: str, day: date) -> str:
    """`text` with `curated` and `curated_hash` set. The body stays byte for byte.

    Raises `ValueError` when the frontmatter block of the page is not a YAML
    mapping.
    """
    return frontmatter.update(text, **{CURATED_KEY: day, HASH_KEY: body_hash(text)})
