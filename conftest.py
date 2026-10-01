"""Test options for every member.

``--fake-now <ISO time>`` moves the clock for the whole run, and the clock
keeps ticking from there. CI runs each suite again on dates that break code
which reads today: the end of a month and a year, a leap day, and a time where
the local date and the UTC date differ. A test that passes only on some days
fails there, on the day it is written.
"""

from __future__ import annotations

from typing import Any

import pytest

_traveller: Any = None


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--fake-now",
        default=None,
        metavar="ISO",
        help="Run every test with the clock moved to this time, for example 2028-02-29T12:00:00Z.",
    )


def pytest_configure(config: pytest.Config) -> None:
    global _traveller
    when = config.getoption("--fake-now")
    if when:
        import time_machine

        _traveller = time_machine.travel(when, tick=True)
        _traveller.start()


def pytest_unconfigure(config: pytest.Config) -> None:
    global _traveller
    if _traveller is not None:
        _traveller.stop()
        _traveller = None
