"""Version strings as numbers, so ``2026.10.1`` sorts above ``2026.9.3``.

A release is ``YYYY.M.N``. The releases before the date scheme were
``0.0.N``. Both are three integers, and an integer compare orders them. A
string compare would put ``2026.10.1`` below ``2026.9.3``, so nothing here
compares strings.
"""

from __future__ import annotations

import re

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse(version: str | None) -> tuple[int, int, int] | None:
    """``(2026, 10, 1)`` for ``2026.10.1`` or ``v2026.10.1``; None for anything else."""
    if not version:
        return None
    match = _VERSION_RE.match(version.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_newer(candidate: str | None, than: str | None) -> bool:
    """True when both parse and ``candidate`` is strictly newer than ``than``."""
    a, b = parse(candidate), parse(than)
    return a is not None and b is not None and a > b


def strip_v(tag: str) -> str:
    """``2026.10.1`` for a tag ``v2026.10.1``."""
    return tag[1:] if tag.startswith("v") else tag
