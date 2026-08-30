import pytest
from joshua_core.__main__ import main
from joshua_shared import config


def test_startup_aborts_on_bad_config(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setenv("JOSHUA_CONFIG", str(tmp_path / "absent.yaml"))
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
