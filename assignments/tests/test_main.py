"""The entrypoint: it refuses to start when the section is off or a setting is bad."""

from __future__ import annotations

import pytest
from joshua_assignments import __main__ as entry
from joshua_shared import config

ON = """
name: Home
timezone: UTC
people:
  - id: alex
    name: Alex
assignments:
  enabled: true
"""


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    def write(text: str):
        path = tmp_path / "joshua.yaml"
        path.write_text(text)
        monkeypatch.setenv("JOSHUA_CONFIG", str(path))
        monkeypatch.setenv("JOSHUA_DATA_DIR", str(tmp_path))
        config._cache = None
        return path

    yield write
    config._cache = None


@pytest.fixture(autouse=True)
def keep_root_logging():
    import logging

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_disabled_exits_without_serving(config_file, monkeypatch, caplog) -> None:
    config_file(ON.replace("enabled: true", "enabled: false"))
    served = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: served.append(a))
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    assert served == []


def test_a_bad_config_exits(config_file, monkeypatch, capsys) -> None:
    config_file("name: Home\n")
    with pytest.raises(SystemExit):
        entry.main()
    assert "config error" in capsys.readouterr().err


def test_a_bad_setting_exits(config_file, monkeypatch, capsys) -> None:
    config_file(ON)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        entry.main()
    assert "DATABASE_URL is required" in capsys.readouterr().err


def test_enabled_serves_on_port_8000(config_file, monkeypatch) -> None:
    config_file(ON)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("ASSIGNMENTS_TOKENS", "alex=a-token")
    monkeypatch.delenv("CHANNELS_URL", raising=False)
    monkeypatch.delenv("JOSHUA_TOKEN_ASSIGNMENTS", raising=False)
    served = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **k: served.append(k))
    entry.main()
    assert served == [{"host": "0.0.0.0", "port": 8000, "log_config": None}]
    from joshua_assignments import server

    server._manager = None
