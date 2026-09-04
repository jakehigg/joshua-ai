import shutil
import subprocess

import pytest
from joshua_core.__main__ import main
from joshua_core.main import _bootstrap_layout
from joshua_shared import config, layout, wikigit
from joshua_shared import config as config_module

_GIT_MISSING = shutil.which("git") is None

CONFIG_YAML = """
name: Test House
timezone: America/New_York
people:
  - id: alex
    name: Alex
"""


def test_startup_aborts_on_bad_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setenv("JOSHUA_CONFIG", str(tmp_path / "absent.yaml"))
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


def test_bootstrap_layout_runs_in_order(monkeypatch, tmp_path):
    """The wiki and `shared/` exist, an older volume is migrated onto the
    wiki before any template page can be written, the shared profile is
    titled only after that, and only then do the docs and each person's tree
    get bootstrapped — the indexer's first pass sees the wiki already
    settled, and a migration never finds a template shadowing a real
    profile. The wiki git sync runs last, once every page is in place."""
    calls: list[str] = []
    monkeypatch.setenv(layout.DATA_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(layout, "bootstrap_wiki", lambda *a, **k: calls.append("wiki"))
    monkeypatch.setattr(layout, "bootstrap_shared", lambda *a, **k: calls.append("shared"))
    monkeypatch.setattr(
        layout,
        "bootstrap_shared_profile",
        lambda name, *a, **k: calls.append(f"shared_profile:{name}"),
    )
    monkeypatch.setattr(
        layout, "migrate_to_one_wiki", lambda *a, **k: calls.append("migrate") or {}
    )
    monkeypatch.setattr(layout, "bootstrap_docs", lambda *a, **k: calls.append("docs") or 0)
    monkeypatch.setattr(
        layout, "bootstrap_person", lambda pid, name, *a, **k: calls.append(f"person:{pid}")
    )
    monkeypatch.setattr(wikigit, "ensure_repo", lambda *a, **k: calls.append("git_init") or False)
    monkeypatch.setattr(wikigit, "commit", lambda *a, **k: calls.append("git_commit") or False)

    settings = config_module.parse(CONFIG_YAML, env={}, source="<test>")
    _bootstrap_layout(settings)

    assert calls == [
        "wiki",
        "shared",
        "migrate",
        "shared_profile:Test House",
        "docs",
        "person:alex",
        "git_init",
        "git_commit",
    ]


@pytest.mark.skipif(_GIT_MISSING, reason="git binary required")
def test_wiki_git_first_start_commits_everything_second_start_commits_nothing(
    monkeypatch, tmp_path
):
    """The first start on an existing wiki commits every page it finds; a
    second start with nothing changed makes no new commit."""
    monkeypatch.setenv(layout.DATA_DIR_ENV, str(tmp_path))
    settings = config_module.parse(CONFIG_YAML, env={}, source="<test>")

    _bootstrap_layout(settings)
    wiki = layout.wiki_root(tmp_path)
    log_after_first = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True, check=True
    ).stdout
    commits_after_first = len(log_after_first.splitlines())
    assert commits_after_first >= 1
    assert (wiki / ".git").is_dir()

    _bootstrap_layout(settings)
    log_after_second = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True, check=True
    ).stdout
    assert len(log_after_second.splitlines()) == commits_after_first
