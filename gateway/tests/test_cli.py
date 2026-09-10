"""``python -m joshua_gateway mcp check`` and ``mcp install``.

The pre-flight a person runs from a shell before they restart the gateway.
"""

from __future__ import annotations

import json
import textwrap

import pytest
from joshua_gateway import cli, mcp_store
from joshua_gateway.__main__ import main

MCP_BLOCK = """\
weather:
  type: stdio
  package: npm:weather-mcp@1.6.1
  allow: all
news:
  type: stdio
  package: pypi:news-mcp==2.0.0
  allow: all
plain:
  type: stdio
  command: mcp-plain
  allow: all
"""

CONFIG = (
    textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
    """)
    + "mcp:\n"
    + textwrap.indent(MCP_BLOCK, "  ")
)


@pytest.fixture
def instance(monkeypatch, tmp_path):
    """A joshua.yaml with three entries and an empty store."""
    from joshua_shared import config

    path = tmp_path / "joshua.yaml"
    path.write_text(CONFIG)
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(path))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    monkeypatch.setenv(mcp_store.STORE_ENV_VAR, str(tmp_path / "store"))
    return tmp_path / "store"


def _record(store, name: str, package: str) -> None:
    entry = store / name
    (entry / "bin").mkdir(parents=True)
    (entry / mcp_store.RECORD_NAME).write_text(
        json.dumps(
            {
                "name": name,
                "package": package,
                "kind": "npm",
                "bare_name": name,
                "registry": None,
                "allow_scripts": False,
                "sha256": None,
                "resolved": "1.6.1",
                "installed_at": "2026-09-08T00:00:00+00:00",
                "bin_dirs": ["bin"],
            }
        )
    )


def test_check_lists_what_is_missing_and_exits_non_zero(instance, capsys):
    assert cli.mcp_command(["check"]) == 1
    out = capsys.readouterr().out
    assert "weather: not installed  npm:weather-mcp@1.6.1" in out
    assert "news: not installed  pypi:news-mcp==2.0.0" in out
    assert "plain" not in out, "an entry with a plain command is not installed by the gateway"
    assert "2 entries need an install" in out


def test_check_is_clean_when_the_store_matches(instance, capsys):
    _record(instance, "weather", "npm:weather-mcp@1.6.1")
    _record(instance, "news", "pypi:news-mcp==2.0.0")
    assert cli.mcp_command(["check"]) == 0
    out = capsys.readouterr().out
    assert "weather: installed" in out
    assert "need an install" not in out


def test_check_says_when_a_spec_changed(instance, capsys):
    _record(instance, "weather", "npm:weather-mcp@1.0.0")
    assert cli.mcp_command(["check", "weather"]) == 1
    assert "weather: changed  npm:weather-mcp@1.6.1" in capsys.readouterr().out


def test_check_names_the_known_servers_for_an_unknown_one(instance):
    with pytest.raises(SystemExit, match="known: news, weather"):
        cli.mcp_command(["check", "nope"])


def test_install_runs_every_entry_the_store_does_not_hold(instance, monkeypatch, capsys):
    installed = []

    def fake_install(request, **kwargs):
        installed.append(request.name)
        return {"resolved": request.spec.version, "installed_at": "now"}

    monkeypatch.setattr(mcp_store, "install", fake_install)
    assert cli.mcp_command(["install"]) == 0
    assert sorted(installed) == ["news", "weather"]
    assert "resolved 1.6.1 at now" in capsys.readouterr().out


def test_install_one_entry_by_name(instance, monkeypatch):
    installed = []
    monkeypatch.setattr(
        mcp_store,
        "install",
        lambda request, **kw: installed.append(request.name) or {"resolved": "x"},
    )
    assert cli.mcp_command(["install", "news"]) == 0
    assert installed == ["news"]


def test_install_reports_a_failure_and_keeps_going(instance, monkeypatch, capsys):
    def fake_install(request, **kwargs):
        if request.name == "weather":
            raise mcp_store.InstallError("npm failed: no such package")
        return {"resolved": "2.0.0", "installed_at": "now"}

    monkeypatch.setattr(mcp_store, "install", fake_install)
    assert cli.mcp_command(["install"]) == 1
    captured = capsys.readouterr()
    assert "no such package" in captured.err
    assert "resolved 2.0.0" in captured.out, "the other entry still installs"


def test_install_reinstalls_when_asked(instance, monkeypatch):
    _record(instance, "weather", "npm:weather-mcp@1.6.1")
    _record(instance, "news", "pypi:news-mcp==2.0.0")
    installed = []
    monkeypatch.setattr(
        mcp_store,
        "install",
        lambda request, **kw: installed.append(request.name) or {"resolved": "x"},
    )
    assert cli.mcp_command(["install", "--reinstall"]) == 0
    assert sorted(installed) == ["news", "weather"]


def test_a_config_with_no_package_entry_says_so(monkeypatch, tmp_path, capsys):
    from joshua_shared import config

    path = tmp_path / "joshua.yaml"
    path.write_text(
        textwrap.dedent("""\
        name: Test
        timezone: America/New_York
        people:
          - id: alex
            name: Alex
        mcp:
          files:
            kind: builtin
            allow: all
        """)
    )
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(path))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    assert cli.mcp_command(["check"]) == 0
    assert cli.mcp_command(["install"]) == 0
    assert capsys.readouterr().out.count("no package servers in joshua.yaml") == 2


def test_a_config_that_does_not_parse_is_one_line(monkeypatch, tmp_path, capsys):
    from joshua_shared import config

    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(tmp_path / "absent.yaml"))
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_cache_path", None)
    assert cli.mcp_command(["check"]) == 1
    assert capsys.readouterr().err.startswith("config error:")


# -- the entrypoint dispatch ---------------------------------------------------


def test_the_entrypoint_routes_mcp_to_the_subcommand(instance):
    with pytest.raises(SystemExit) as exc:
        main(["mcp", "check"])
    assert exc.value.code == 1  # nothing installed yet


def test_an_unknown_command_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["serve-please"])
    assert exc.value.code == 2
    assert "unknown command" in capsys.readouterr().err
