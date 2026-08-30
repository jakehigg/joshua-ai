"""Refuse a chart whose version and appVersion disagree.

The chart and the three containers ship together, and an interface between them
can change in any release before 1.0. A chart that meets an image it did not
ship with is a broken install, so the two versions are always the same.

The release workflow packages the chart with `--version` and `--app-version`
set from the tag, so a release is right whatever this file says. This check
keeps the file itself honest, because `image.tag` defaults to `appVersion` and
a person who installs from a Git checkout gets what is written here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parent.parent / "charts" / "joshua" / "Chart.yaml"


def main() -> int:
    chart = yaml.safe_load(CHART.read_text())
    version = str(chart.get("version", ""))
    app_version = str(chart.get("appVersion", ""))
    if version != app_version:
        print(
            f"chart-version: version {version!r} and appVersion {app_version!r} "
            f"disagree in {CHART.name}. Set both to the version you release.",
            file=sys.stderr,
        )
        return 1
    print(f"chart-version: clean ({version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
