"""Guard: a kernel prompt file must name only kernel tools.

The kernel prompt must not reference the legacy tools or claim web
access the agent does not have. This grep runs over every packaged
prompt file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PROMPTS_DIR = Path(__file__).parent.parent / "joshua_core" / "prompts" / "builtin"

# (label, compiled pattern). Product names and phrases match case-insensitively;
# the SDK built-in tool names match as whole words, case-sensitively, so
# ``read_file`` and words like "already" do not trip them.
_FORBIDDEN = [
    ("home assistant", re.compile(r"home\s*assistant", re.IGNORECASE)),
    ("reminders", re.compile(r"reminders", re.IGNORECASE)),
    ("pantry", re.compile(r"pantry", re.IGNORECASE)),
    ("ops", re.compile(r"\bops\b", re.IGNORECASE)),
    ("memos", re.compile(r"\bmemos\b", re.IGNORECASE)),
    ("wiki.js", re.compile(r"wiki\.js", re.IGNORECASE)),
    ("browse the web", re.compile(r"browse the web", re.IGNORECASE)),
    ("Read", re.compile(r"\bRead\b")),
    ("WebSearch", re.compile(r"\bWebSearch\b")),
    ("WebFetch", re.compile(r"\bWebFetch\b")),
]

_FILES = sorted(_PROMPTS_DIR.glob("*.md"))


def test_kernel_prompt_files_exist() -> None:
    names = {p.name for p in _FILES}
    assert {
        "base.md",
        "people.md",
        "chat.md",
        "group.md",
        "guest.md",
        "event.md",
        "scheduled.md",
    } <= names


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_no_forbidden_terms(path: Path) -> None:
    text = path.read_text()
    hits = [label for label, pattern in _FORBIDDEN if pattern.search(text)]
    assert not hits, f"{path.name} names non-kernel terms: {hits}"


# Every kernel file stays short, because the whole set is prepended to every
# turn. ``people.md`` carries the storage rules — which of the wiki, the
# journal, and a profile a thing belongs in — and is the one file where an
# extra line buys guidance the agent cannot work out for itself.
_LINE_BUDGET = 80
_BUDGETS = {"people.md": 90}


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_file_within_line_budget(path: Path) -> None:
    budget = _BUDGETS.get(path.name, _LINE_BUDGET)
    lines = len(path.read_text().splitlines())
    assert lines <= budget, f"{path.name} is {lines} lines, over its {budget}-line budget"
