"""The shape rule in `scripts/check_chart_version.py`.

The version is a date, ``YYYY.M.N``. Helm requires SemVer, so a leading zero is
refused, and a version always has three parts.
"""

from __future__ import annotations

import pytest
from check_chart_version import version_shape_ok


@pytest.mark.parametrize("version", ["2026.10.1", "2026.1.1", "2027.12.14", "2026.10.20"])
def test_accepts_the_date_shape(version: str) -> None:
    assert version_shape_ok(version)


@pytest.mark.parametrize(
    "version",
    ["2026.10.01", "2026.01.1", "2026.10", "2026.10.0", "0.0.9", "v2026.10.1", "2026.13.1", ""],
)
def test_refuses_another_shape(version: str) -> None:
    assert not version_shape_ok(version)
