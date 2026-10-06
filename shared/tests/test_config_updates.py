"""The ``updates:`` section: what the instance builtin reads, and its two repositories."""

from __future__ import annotations

import pytest
from joshua_shared import config

BASE = "name: Test\ntimezone: UTC\npeople:\n  - id: alex\n    name: Alex\n"


def test_the_default_names_both_repositories_and_is_on() -> None:
    cfg = config.parse(BASE, {}, source="t.yaml")
    assert cfg.updates.enabled is True
    assert cfg.updates.repos == {
        "joshua-ai": "jakehigg/joshua-ai",
        "joshua-addons": "jakehigg/joshua-addons",
    }


def test_a_fork_names_its_own_repositories() -> None:
    text = (
        BASE + "updates:\n  enabled: false\n  repos:\n    joshua-ai: a/b\n    joshua-addons: a/c\n"
    )
    cfg = config.parse(text, {}, source="t.yaml")
    assert cfg.updates.enabled is False
    assert cfg.updates.repos["joshua-ai"] == "a/b"


@pytest.mark.parametrize(
    "repos",
    [
        "    joshua-ai: a/b\n",  # no joshua-addons
        "    joshua-ai: not-a-repo\n    joshua-addons: a/c\n",
        "    joshua-ai: https://github.com/a/b\n    joshua-addons: a/c\n",
    ],
)
def test_a_bad_repos_block_is_refused(repos: str) -> None:
    with pytest.raises(config.ConfigError):
        config.parse(BASE + "updates:\n  repos:\n" + repos, {}, source="t.yaml")


def test_instance_is_a_valid_builtin_name() -> None:
    cfg = config.parse(BASE + "mcp:\n  instance:\n    kind: builtin\n", {}, source="t.yaml")
    assert cfg.mcp["instance"].kind == "builtin"
