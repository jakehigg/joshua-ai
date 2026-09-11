"""The packaged kernel prompt files: they exist, and they stay short.

Every file here is prepended to every turn, so length is a cost each turn
pays. The agent's lack of SDK built-in tools is not checked here: it is
asserted at runtime in ``engine.agent`` and tested in ``test_agent_options``,
where a widened tool list fails for real rather than by a grep over prose.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PROMPTS_DIR = Path(__file__).parent.parent / "joshua_core" / "prompts" / "builtin"
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


# Every kernel file stays short, because the whole set is prepended to every
# turn. ``people.md`` carries the storage rules — which of the wiki, the
# journal, and a profile a thing belongs in — and is the one file where an
# extra line buys guidance the agent cannot work out for itself.
_LINE_BUDGET = 80
# people.md carries the skill format the agent writes: the trigger, ``for``,
# and ``kind``. The format grew when a skill gained an audience and a kind, so
# the budget grew with it. Every line here is paid on every turn: cut before
# you raise this again.
_BUDGETS = {"people.md": 98}


@pytest.mark.parametrize("path", _FILES, ids=lambda p: p.name)
def test_file_within_line_budget(path: Path) -> None:
    budget = _BUDGETS.get(path.name, _LINE_BUDGET)
    lines = len(path.read_text().splitlines())
    assert lines <= budget, f"{path.name} is {lines} lines, over its {budget}-line budget"
