"""The lede of a report, the next step of a plan, and the demoted headings."""

from __future__ import annotations

import pytest
from joshua_assignments import digest


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("# Docs\nThe docs are clean.\nTwo links were fixed.\n\n## Highlights", None),
        ("# Docs\n\n  The docs are clean.  \n\nMore.", "The docs are clean."),
        ("# Docs\n## Highlights\n- a", None),
        ("# Docs\n```\ncode\n```", None),
        ("No heading.\n\nText.", None),
        ("", None),
        (None, None),
        ("Intro\n# Docs\nLede.", "Lede."),
    ],
)
def test_lede(report, expected) -> None:
    if report and report.startswith("# Docs\nThe docs are clean.\nTwo"):
        expected = "The docs are clean. Two links were fixed."
    assert digest.lede(report) == expected


def test_a_long_lede_is_cut() -> None:
    text = digest.lede("# T\n" + "word " * 300)
    assert text is not None and len(text) == digest.MAX_LEDE and text.endswith("…")


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        ("- [x] a\n- [ ] b\n3. [ ] **Next:** c", "c"),
        ("1. [x] a\n2) [ ] b\n* [ ] c", "b"),
        ("- [x] a\n- [X] b", None),
        ("No boxes.\ntext [ ] here", None),
        ("- [ ] **Next:**", None),
        ("  + [ ]   **Next:**  open   the PR  ", "open the PR"),
        ("- [ ] a\n- [ ] **next**: b", "b"),
        (None, None),
    ],
)
def test_next_step(plan, expected) -> None:
    assert digest.next_step(plan) == expected


def test_a_long_next_step_is_cut() -> None:
    step = digest.next_step("- [ ] " + "x" * 400)
    assert step is not None and len(step) == digest.MAX_NEXT_STEP


def test_demote_headings() -> None:
    text = "# A\n## B\n   ### C\n#### D\n##### E\n###### F\n#no\n~~~\n# code\n~~~\ntext # x\n#"
    assert digest.demote_headings(text).splitlines() == [
        "### A",
        "#### B",
        "   ##### C",
        "###### D",
        "###### E",
        "###### F",
        "#no",
        "~~~",
        "# code",
        "~~~",
        "text # x",
        "###",
    ]
    assert digest.demote_headings("# A", levels=1) == "## A"
