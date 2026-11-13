"""`scripts/profiles.sh`: the compose profiles that joshua.yaml turns on.

The Makefile exports its output as COMPOSE_PROFILES, so `make up` starts the
viewer and the assignments container when the config enables them. The
script never touches Docker, so the tests run it against a temporary file.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "profiles.sh"


def run(tmp_path: Path, text: str | None) -> str:
    yaml_file = tmp_path / "joshua.yaml"
    if text is not None:
        yaml_file.write_text(text)
    result = subprocess.run([str(SCRIPT), str(yaml_file)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_no_file_prints_nothing(tmp_path: Path) -> None:
    assert run(tmp_path, None) == ""


def test_nothing_enabled_prints_an_empty_line(tmp_path: Path) -> None:
    text = "viewer:\n  enabled: false\nassignments:\n  enabled: false\n"
    assert run(tmp_path, text) == "\n"


def test_the_viewer(tmp_path: Path) -> None:
    text = "viewer:   # the web app\n  enabled: true   # on\n  auth:\n    method: pin\n"
    assert run(tmp_path, text) == "viewer\n"


def test_both(tmp_path: Path) -> None:
    text = "assignments:\n  enabled: true\nviewer:\n  enabled: true\n"
    assert run(tmp_path, text) == "viewer,assignments\n"


def test_an_enabled_key_deeper_or_elsewhere_does_not_count(tmp_path: Path) -> None:
    text = (
        "memory:\n  enabled: true\n"
        "viewer:\n  auth:\n    enabled: true\n  enabled: false\n"
        "assignments:\n  platforms:\n    enabled: true\n"
        "# viewer:\n#   enabled: true\n"
    )
    assert run(tmp_path, text) == "\n"


def test_the_example_config_turns_nothing_on() -> None:
    result = subprocess.run(
        [str(SCRIPT), str(ROOT / "joshua.example.yaml")], capture_output=True, text=True
    )
    assert result.stdout == "\n"
