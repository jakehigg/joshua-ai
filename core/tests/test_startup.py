import pytest
from joshua_core.__main__ import main
from joshua_core.main import _bootstrap_layout
from joshua_shared import config, layout
from joshua_shared import config as config_module

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


def test_bootstrap_layout_runs_in_order(monkeypatch):
    """The wiki and `shared/` exist, an older volume is migrated onto the
    wiki before any template page can be written, the shared profile is
    titled only after that, and only then do the docs and each person's tree
    get bootstrapped — the indexer's first pass sees the wiki already
    settled, and a migration never finds a template shadowing a real
    profile."""
    calls: list[str] = []
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

    settings = config_module.parse(CONFIG_YAML, env={}, source="<test>")
    _bootstrap_layout(settings)

    assert calls == [
        "wiki",
        "shared",
        "migrate",
        "shared_profile:Test House",
        "docs",
        "person:alex",
    ]
