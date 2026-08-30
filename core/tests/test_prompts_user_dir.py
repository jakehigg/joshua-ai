"""Unit tests for the ``user/`` snippet auto-load in ``PromptComposer``.

Offline: the composer reads a temporary prompts tree, so no DB and no SDK. Each
test builds a ``builtin/`` copy of the packaged kernel files plus a ``user/``
directory, so it controls the ``user/`` contents.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from joshua_core.engine.profiles import derive_profile
from joshua_core.engine.prompts import PromptComposer
from joshua_core.store.models import Channel, Person

_PACKAGED_BUILTIN = Path(__file__).parent.parent / "joshua_core" / "prompts" / "builtin"

_MEMBER = Person(id="alex", display_name="Alex", role="member")
_GUEST = Person(id="sam", display_name="Sam", role="guest")
_DM = Channel(id="telegram:1", channel_type="telegram", session_mode="per_person")
_SHARED = Channel(id="telegram:g", channel_type="telegram", session_mode="shared")

# The five profiles a self-hoster snippet must reach: (person, channel, kind).
_PROFILE_CASES = [
    ("dm", _MEMBER, _DM, "message"),
    ("group", None, _SHARED, "message"),
    ("guest", _GUEST, _DM, "message"),
    ("event", _MEMBER, _DM, "event"),
    ("scheduled", _MEMBER, _DM, "task"),
]


def _make_tree(tmp_path: Path) -> Path:
    """Build a prompts tree with a real ``builtin/`` and an empty ``user/``."""
    prompts = tmp_path / "prompts"
    shutil.copytree(_PACKAGED_BUILTIN, prompts / "builtin")
    (prompts / "user").mkdir()
    return prompts


def test_empty_user_dir_is_byte_identical_to_packaged(tmp_path: Path) -> None:
    prompts = _make_tree(tmp_path)
    profile = derive_profile(_MEMBER, _DM)
    from_tree = PromptComposer(prompts).compose(profile, _MEMBER, _DM)
    from_default = PromptComposer().compose(profile, _MEMBER, _DM)
    assert from_tree == from_default


def test_one_user_file_is_appended(tmp_path: Path) -> None:
    prompts = _make_tree(tmp_path)
    (prompts / "user" / "house.md").write_text("Keep replies short.")
    prompt = PromptComposer(prompts).compose(derive_profile(_MEMBER, _DM), _MEMBER, _DM)
    assert "Keep replies short." in prompt


@pytest.mark.parametrize("name", [c[0] for c in _PROFILE_CASES])
def test_user_file_reaches_every_profile(tmp_path: Path, name: str) -> None:
    _, person, channel, kind = next(c for c in _PROFILE_CASES if c[0] == name)
    prompts = _make_tree(tmp_path)
    (prompts / "user" / "house.md").write_text("House rule alpha.")
    profile = derive_profile(person, channel, kind=kind)
    prompt = PromptComposer(prompts).compose(profile, person, channel)
    assert "House rule alpha." in prompt


def test_two_user_files_append_in_sorted_order(tmp_path: Path) -> None:
    prompts = _make_tree(tmp_path)
    (prompts / "user" / "20-second.md").write_text("SECOND snippet.")
    (prompts / "user" / "10-first.md").write_text("FIRST snippet.")
    prompt = PromptComposer(prompts).compose(derive_profile(_MEMBER, _DM), _MEMBER, _DM)
    assert prompt.index("FIRST snippet.") < prompt.index("SECOND snippet.")


def test_non_md_and_readme_are_ignored(tmp_path: Path) -> None:
    prompts = _make_tree(tmp_path)
    (prompts / "user" / "README.md").write_text("Explains the convention.")
    (prompts / "user" / "notes.txt").write_text("Plain text, not loaded.")
    (prompts / "user" / ".hidden.md").write_text("Dotfile, not loaded.")
    prompt = PromptComposer(prompts).compose(derive_profile(_MEMBER, _DM), _MEMBER, _DM)
    assert "Explains the convention." not in prompt
    assert "Plain text, not loaded." not in prompt
    assert "Dotfile, not loaded." not in prompt


def test_user_snippets_land_before_identity_block(tmp_path: Path) -> None:
    prompts = _make_tree(tmp_path)
    (prompts / "user" / "house.md").write_text("House rule alpha.")
    prompt = PromptComposer(prompts).compose(derive_profile(_MEMBER, _DM), _MEMBER, _DM)
    assert prompt.index("House rule alpha.") < prompt.index("## Who you are talking to")
