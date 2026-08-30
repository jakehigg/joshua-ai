"""Identifier rules, and the import invariant `ids` exists for.

`ids` imports nothing from the package. `config` and `layout` both read the
pattern from here, so importing `joshua_shared` never imports `config`. That is
what keeps `python -m joshua_shared.config` free of a RuntimeWarning.
Nothing enforced it before, which is how the warning appeared in the first place.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from joshua_shared.ids import PERSON_ID_PATTERN, PERSON_ID_RE

MINIMAL = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
"""


@pytest.mark.parametrize("value", ["alex", "j", "a-b-c", "person2", "0", "a" * 32])
def test_a_good_id_matches(value: str) -> None:
    assert PERSON_ID_RE.match(value)


@pytest.mark.parametrize(
    "value",
    ["", "-alex", "Alex", "sam_smith", "alex.smith", "alex smith", "a" * 33, "alex\n"],
)
def test_a_bad_id_does_not_match(value: str) -> None:
    assert not PERSON_ID_RE.match(value)


def test_the_compiled_form_matches_the_pattern() -> None:
    assert PERSON_ID_RE.pattern == PERSON_ID_PATTERN


def test_importing_the_package_does_not_import_config() -> None:
    """`config` must stay out of `sys.modules` after the package import.

    `python -m joshua_shared.config` warns when the package pulls `config` in
    first. This is the cheap check for that.
    """
    code = "import joshua_shared, sys; print('joshua_shared.config' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


def test_the_config_command_runs_with_no_warning(tmp_path: Path) -> None:
    """`python -m joshua_shared.config validate` must print nothing on stderr."""
    config = tmp_path / "joshua.yaml"
    config.write_text(MINIMAL)
    result = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::RuntimeWarning",
            "-m",
            "joshua_shared.config",
            "validate",
            str(config),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
