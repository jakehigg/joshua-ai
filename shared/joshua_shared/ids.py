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

# A ULID: 26 characters of Crockford base32, upper case. The first character
# is 0-7, because a ULID holds 128 bits and 26 characters hold 130.
ULID_PATTERN = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}\Z"
ULID_RE = re.compile(ULID_PATTERN)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def is_ulid(value: str) -> bool:
    """True when ``value`` is a ULID in canonical upper-case form."""
    return bool(ULID_RE.match(value))


def new_ulid(ms: int, entropy: bytes) -> str:
    """Return the ULID for ``ms`` (Unix time in milliseconds) and 10 bytes of ``entropy``.

    The caller gives the time and the random bytes, so a test gets a fixed id.
    Use ``int(time.time() * 1000)`` and ``os.urandom(10)`` in production.
    """
    if not 0 <= ms < 1 << 48:
        raise ValueError("ULID time must fit in 48 bits")
    if len(entropy) != 10:
        raise ValueError("ULID entropy must be 10 bytes")
    value = (ms << 80) | int.from_bytes(entropy, "big")
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))
