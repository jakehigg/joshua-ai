"""The tracking keys: how the curator knows which passes it ran on a page.

A page carries one pair of frontmatter keys for each pass that ran on it:

```yaml
curated: 2026-10-19            # the local day of the run that placed the page
curated_hash: 7f3a9c1e04b2     # the body hash at that pass
refreshed: 2026-10-19          # the local day of the run that refreshed it
refreshed_hash: 7f3a9c1e04b2   # the body hash at that pass
```

`curated` goes on a page when `place` ran on it, or when the page needs no
`place`: a page that is not a topic page, such as a journal entry. `refreshed`
goes on a topic page when its `refresh` pass ran. Each pair is written on its
own, so a run that the budget stopped keeps the passes it did.

The body hash is the first 12 hex characters of the SHA-256 of the body
(the text after the frontmatter block), with trailing whitespace removed. A
change to the frontmatter alone does not change it. A change to the body
makes both pairs old.

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
REFRESHED_KEY = "refreshed"
REFRESHED_HASH_KEY = "refreshed_hash"
# Every key the curator writes in the frontmatter of a page.
TRACKING_KEYS = (CURATED_KEY, HASH_KEY, REFRESHED_KEY, REFRESHED_HASH_KEY)
HASH_CHARS = 12
# The top-level folders the curator never writes.
UNTOUCHED_FOLDERS = ("people", "joshua-docs")


def body_hash(text: str) -> str:
    """The hash of the body of `text`. See the module docstring."""
    _, body = frontmatter.split(text)
    return hashlib.sha256(body.rstrip().encode("utf-8")).hexdigest()[:HASH_CHARS]


def _is_current(text: str, key: str) -> bool:
    meta, _ = frontmatter.split(text)
    recorded = meta.get(key)
    return isinstance(recorded, str) and recorded == body_hash(text)


def needs_place(text: str) -> bool:
    """True when the page has no `curated_hash`, or its body changed since."""
    return not _is_current(text, HASH_KEY)


def needs_refresh(text: str) -> bool:
    """True when the page has no `refreshed_hash`, or its body changed since."""
    return not _is_current(text, REFRESHED_HASH_KEY)


def needs_review(text: str) -> bool:
    """The same as `needs_place`: the `curated` pair is old or missing."""
    return needs_place(text)


def _day(text: str, key: str) -> date | None:
    meta, _ = frontmatter.split(text)
    value = meta.get(key)
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def curated_day(text: str) -> date | None:
    """The `curated` day of a page, or None when it has none."""
    return _day(text, CURATED_KEY)


def refreshed_day(text: str) -> date | None:
    """The `refreshed` day of a page, or None when it has none."""
    return _day(text, REFRESHED_KEY)


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


def with_tracking(text: str, day: date, *, curated: bool = True, refreshed: bool = False) -> str:
    """`text` with the `curated` pair, the `refreshed` pair, or both set to
    `day` and the body hash. The body stays byte for byte.

    Raises `ValueError` when the frontmatter block of the page is not a YAML
    mapping.
    """
    keys: dict[str, Any] = {}
    digest = body_hash(text)
    if curated:
        keys.update({CURATED_KEY: day, HASH_KEY: digest})
    if refreshed:
        keys.update({REFRESHED_KEY: day, REFRESHED_HASH_KEY: digest})
    if not keys:
        return text
    return frontmatter.update(text, **keys)
