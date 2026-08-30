"""Identifier rules shared by the config loader and the data layout.

This module imports nothing from the package. ``config`` and ``layout`` both read
the pattern from here, so importing ``joshua_shared`` never imports ``config``
and ``python -m joshua_shared.config`` runs with no warning.
"""

from __future__ import annotations

import re

# `\Z` and not `$`: `$` also matches before a trailing newline, so `"alex\n"`
# would pass and become a directory name. Every caller uses `PERSON_ID_RE`.
PERSON_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}\Z"
PERSON_ID_RE = re.compile(PERSON_ID_PATTERN)
