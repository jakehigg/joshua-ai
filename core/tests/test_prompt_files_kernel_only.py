"""Guard: a shipped prompt must name only what this repository builds.

The kernel prompt must not reference the tools of the deployment Joshua grew
out of, nor claim a capability the agent does not have. The grep runs over
every packaged prompt file AND over the strings composed around them in
``engine.prompts`` and ``memory.nightly``, because a claim in a Python
constant reaches the model exactly as a claim in a Markdown file does.
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
    # Identity comes from a verified account. There is no voice channel.
    ("recognized voice", re.compile(r"recogni[sz]ed voice", re.IGNORECASE)),
    # "Shared systems" named the appliances of another deployment. Here a
    # person reaches what the gateway's per-person policy allows, nothing more.
    ("shared systems", re.compile(r"shared systems?\b", re.IGNORECASE)),
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


def _composed() -> str:
    """The prompt a member's turn actually gets, files and constants together."""
    from joshua_core.engine.profiles import derive_profile
    from joshua_core.engine.prompts import PromptComposer
    from joshua_core.store.models import Channel, Person

    person = Person(id="alex", display_name="Alex", role="member")
    guest = Person(id="sam", display_name="Sam", role="guest")
    dm = Channel(id="telegram:1", channel_type="telegram", session_mode="per_person")
    composer = PromptComposer()
    return composer.compose(derive_profile(person, dm), person, dm) + composer.compose(
        derive_profile(guest, dm), guest, dm
    )


def test_the_composed_prompt_names_nothing_unbuilt() -> None:
    """The .md files are only half of what reaches the model. The identity
    block, the tools block and the guest block are Python constants, and they
    carried the claims this guard exists to stop."""
    text = _composed()
    hits = [label for label, pattern in _FORBIDDEN if pattern.search(text)]
    assert not hits, f"the composed prompt names non-kernel terms: {hits}"


def test_the_reflection_prompts_name_nothing_unbuilt() -> None:
    """The nightly prompts reach a model too, with no file to grep."""
    from joshua_core.memory.nightly import JOURNAL_SYSTEM, PROFILE_SYSTEM, SHARED_SYSTEM

    text = "\n".join((JOURNAL_SYSTEM, PROFILE_SYSTEM, SHARED_SYSTEM))
    hits = [label for label, pattern in _FORBIDDEN if pattern.search(text)]
    assert not hits, f"a reflection prompt names non-kernel terms: {hits}"


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
