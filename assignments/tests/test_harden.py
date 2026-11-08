"""The manager process becomes non-dumpable on Linux, and nothing changes elsewhere."""

from __future__ import annotations

import subprocess
import sys

import pytest
from joshua_assignments import harden

linux_only = pytest.mark.skipif(
    sys.platform != "linux", reason="prctl(PR_SET_DUMPABLE) is Linux only (#75)"
)


class FakeLibc:
    def __init__(self, result: int = 0) -> None:
        self.result = result
        self.calls: list[tuple] = []

    def prctl(self, *args):
        self.calls.append(args)
        return self.result


def test_another_system_is_a_no_op_that_logs(caplog) -> None:
    libc = FakeLibc()
    with caplog.at_level("INFO"):
        assert harden.make_non_dumpable("darwin", libc) is False
    assert libc.calls == []
    assert "stays dumpable" in caplog.text


def test_linux_calls_prctl_set_dumpable_zero() -> None:
    libc = FakeLibc()
    assert harden.make_non_dumpable("linux", libc) is True
    assert libc.calls == [(harden.PR_SET_DUMPABLE, 0, 0, 0, 0)]
    assert harden.get_dumpable(FakeLibc(1)) == 1


def test_a_refused_prctl_raises() -> None:
    with pytest.raises(OSError, match="PR_SET_DUMPABLE"):
        harden.make_non_dumpable("linux", FakeLibc(-1))


@linux_only
def test_the_real_call_makes_the_process_non_dumpable() -> None:
    # In a child, so this test process stays dumpable.
    code = (
        "from joshua_assignments import harden; "
        "assert harden.get_dumpable() == 1; "
        "assert harden.make_non_dumpable() is True; "
        "print(harden.get_dumpable())"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "0"
