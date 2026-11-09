"""Short texts from the plan and the report of an assignment, and one rewrite.

The read API gives each list item a ``lede`` (from the report) and a
``next_step`` (from the plan), so a list needs no detail. The runner-made
report puts the plan under a section of its own, so ``demote_headings``
moves each heading of the plan down two levels: a plan heading such as
``## Outputs`` then cannot take the place of a section of the report.
Each function is pure.
"""

from __future__ import annotations

import re

MAX_LEDE = 500
MAX_NEXT_STEP = 300
# ``**Next:**``, and the forms that the viewer also takes: ``**Next**:``, any case.
_NEXT_RE = re.compile(r"\*\*\s*next\s*:?\s*\*\*\s*:?\s*", re.IGNORECASE)
# A heading line: up to three spaces, one to six ``#``, then a space or the end.
_HEADING_RE = re.compile(r"^( {0,3})(#{1,6})(?=\s|$)")
_FENCE_RE = re.compile(r"^ {0,3}(```|~~~)")
# An unticked check box of a list item: ``- [ ] step`` or ``2. [ ] step``.
_UNTICKED_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[ \]\s*(.*)$")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def lede(report: str | None) -> str | None:
    """The first paragraph under the H1 of ``report``, on one line, or None."""
    lines = (report or "").splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("# ")), None)
    if start is None:
        return None
    paragraph: list[str] = []
    for line in lines[start + 1 :]:
        text = line.strip()
        if not text:
            if paragraph:
                break
            continue
        if _HEADING_RE.match(line) or _FENCE_RE.match(line):
            break
        paragraph.append(text)
    joined = " ".join(paragraph)
    return _cut(joined, MAX_LEDE) if joined else None


def next_step(plan: str | None) -> str | None:
    """The next step of ``plan``, with no check box and no marks, or None.

    It is the first unticked line that holds ``**Next:**``, else the first
    unticked line.
    """
    unticked = [
        match.group(1).strip()
        for line in (plan or "").splitlines()
        if (match := _UNTICKED_RE.match(line))
    ]
    marked = next((step for step in unticked if _NEXT_RE.search(step)), None)
    step = marked if marked is not None else next(iter(unticked), None)
    if step is None:
        return None
    text = " ".join(_NEXT_RE.sub(" ", step).split())
    return _cut(text, MAX_NEXT_STEP) if text else None


def demote_headings(markdown: str, levels: int = 2) -> str:
    """``markdown`` with each heading ``levels`` deeper, at most ``######``.

    A line inside a fenced code block is not a heading and stays as it is.
    """
    out: list[str] = []
    fenced = False
    for line in markdown.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
        elif not fenced and (match := _HEADING_RE.match(line)):
            depth = min(len(match.group(2)) + levels, 6)
            line = f"{match.group(1)}{'#' * depth}{line[match.end() :]}"
        out.append(line)
    return "\n".join(out)
