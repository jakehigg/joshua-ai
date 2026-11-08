"""The working clock: a budget of seconds that can pause."""

from __future__ import annotations

from joshua_assignments.clock import Clock


def test_the_clock_counts_work_and_not_pauses() -> None:
    now = [100.0]
    clock = Clock(60, now=lambda: now[0])
    now[0] = 110
    assert clock.elapsed() == 10 and clock.remaining() == 50
    clock.pause()
    clock.pause()
    assert clock.paused is True
    now[0] = 200
    assert clock.elapsed() == 10
    clock.resume()
    clock.resume()
    now[0] = 260
    assert clock.elapsed() == 70 and clock.remaining() == -10


def test_a_start_can_be_given() -> None:
    assert Clock(5, start=0.0, now=lambda: 2.0).remaining() == 3
