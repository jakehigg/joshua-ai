"""A shipped script must never put a credential in a command-line argument.

Every user on a machine can read the argument list of every process, so
``curl -H "Authorization: Bearer $TOKEN"`` shows the token to all of them.
``curl --config -`` reads the header from stdin, which no other user can see.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((ROOT / "scripts").glob("*.sh"))

# `-H` or `--header` carrying an Authorization value on the command line.
_HEADER_ARG = re.compile(r"(?:-H|--header)\s+[\"']?Authorization:", re.IGNORECASE)

# A token variable used as an argument to any command.
_TOKEN_ARG = re.compile(r"(?:-H|--header|-u|--user)\s+[\"'][^\"']*\$\{?JOSHUA_TOKEN", re.IGNORECASE)


def test_there_are_scripts_to_check() -> None:
    """Guard the guard: a bad glob would make every test below vacuous."""
    assert SCRIPTS, "no scripts/*.sh found"


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_authorization_header_in_an_argument(script: Path) -> None:
    text = script.read_text()
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue  # a comment explains the rule; it does not break it
        assert not _HEADER_ARG.search(line), (
            f"{script.name}:{number} puts an Authorization header in an argument. "
            f"Send it on stdin: printf 'header = \"Authorization: Bearer %s\"\\n' "
            f'"$TOKEN" | curl --config - ...'
        )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_fleet_token_in_an_argument(script: Path) -> None:
    text = script.read_text()
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        assert not _TOKEN_ARG.search(line), (
            f"{script.name}:{number} puts a JOSHUA_TOKEN in an argument, "
            f"where every user on the machine reads it in `ps`."
        )
