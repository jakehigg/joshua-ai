"""Refuse a tree where the version is not the same in every file that holds it,
or is not of the date shape.

The chart, the three images, and the compose file ship together, and an
interface between them can change in any release. A chart or a compose file
that meets an image it did not ship with is a broken install.

The version is a date: ``YYYY.M.N``, the year, the month with no leading zero,
and the sequence of the release in that month, from 1. ``2026.10.1`` is the
first release in October 2026. Two constraints give the shape: Helm requires
SemVer, so no part has a leading zero; and the version always has three parts,
because an unquoted ``2026.10`` is a number in YAML.

Four values must agree:

* ``charts/joshua/Chart.yaml`` ``version`` and ``appVersion``
* ``.env.example`` ``JOSHUA_VERSION``, which ``make init-env`` writes into a
  person's ``.env`` and which pins their installation
* the ``JOSHUA_VERSION`` default in ``docker-compose.yml``

The release workflow packages the chart and tags the images from the Git tag,
so a release is right whatever these files say. This check keeps the files
honest, because a person who installs from a checkout gets what is written
here.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "charts" / "joshua" / "Chart.yaml"
ENV_EXAMPLE = ROOT / ".env.example"
COMPOSE = ROOT / "docker-compose.yml"

# YYYY.M.N: a four-digit year, a month 1-12 with no leading zero, a sequence from 1.
VERSION_SHAPE = re.compile(r"^\d{4}\.(1[0-2]|[1-9])\.[1-9]\d*$")
SHAPE_HELP = (
    "The version is YYYY.M.N: the year, the month with no leading zero, and the "
    "sequence of the release in that month, from 1. Example: 2026.10.1."
)


def version_shape_ok(version: str) -> bool:
    """True when ``version`` is of the ``YYYY.M.N`` shape."""
    return VERSION_SHAPE.match(version) is not None


def _env_example_version() -> str | None:
    match = re.search(r"(?m)^JOSHUA_VERSION=(.+)$", ENV_EXAMPLE.read_text())
    return match.group(1).strip() if match else None


def _compose_default() -> str | None:
    """The fallback in ``${JOSHUA_VERSION:-<default>}``. Every image shares it."""
    defaults = set(re.findall(r"\$\{JOSHUA_VERSION:-([^}]+)\}", COMPOSE.read_text()))
    if len(defaults) != 1:
        return None
    return defaults.pop().strip()


def main() -> int:
    import yaml

    chart = yaml.safe_load(CHART.read_text())
    found = {
        "charts/joshua/Chart.yaml version": str(chart.get("version", "")),
        "charts/joshua/Chart.yaml appVersion": str(chart.get("appVersion", "")),
        ".env.example JOSHUA_VERSION": _env_example_version(),
        "docker-compose.yml JOSHUA_VERSION default": _compose_default(),
    }
    missing = [name for name, value in found.items() if not value]
    if missing:
        print("chart-version: cannot read " + ", ".join(missing), file=sys.stderr)
        return 1
    if len(set(found.values())) != 1:
        print("chart-version: the version is not the same everywhere.", file=sys.stderr)
        for name, value in found.items():
            print(f"  {value:>12}  {name}", file=sys.stderr)
        print("Set all of them to the version you release.", file=sys.stderr)
        return 1
    version = next(iter(found.values()))
    if not version_shape_ok(version):
        print(f"chart-version: '{version}' is not of the date shape.", file=sys.stderr)
        print(SHAPE_HELP, file=sys.stderr)
        return 1
    print(f"chart-version: clean ({version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
