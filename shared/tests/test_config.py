import subprocess
import sys
from pathlib import Path

import pytest
from joshua_shared import config

ROOT = Path(__file__).resolve().parents[2]

GOOD = """
name: Home
timezone: America/New_York
people:
  - id: alex
    name: Alex
    handles:
      telegram: "998877"
      imessage: "+15551234567"
  - id: mia
    name: Mia
    role: guest
    handles:
      telegram: "112233"
groups:
  - id: everyone
    channel: imessage
    chat_id: "iMessage;+;chat100000000000000001"
    members:
      - alex
      - mia

channels:
  telegram:
    bot_token: ${TELEGRAM_BOT_TOKEN}
  destinations:
    everyone: imessage:group:everyone
"""

ENV = {"TELEGRAM_BOT_TOKEN": "123:abc"}


def parse_good(**overrides: str) -> config.JoshuaConfig:
    return config.parse(GOOD, ENV, source="test.yaml")


def test_happy_path_and_defaults() -> None:
    cfg = config.parse(GOOD, ENV, source="test.yaml")
    assert cfg.name == "Home"
    assert len(cfg.people) == 2
    assert cfg.channels.telegram is not None
    assert cfg.channels.telegram.bot_token == "123:abc"
    # Every channel drops an unknown sender by default. A reply is opt-in.
    assert cfg.channels.telegram.unknown_sender == "drop"
    # Defaults fill in when a section is absent.
    assert cfg.core.model == "claude-opus-5"
    assert cfg.core.scheduler_tick_seconds == 15
    assert cfg.memory.inject.top_k == 2
    assert cfg.channels.webhooks.allowed_callers == ["laptop", "ci"]


def test_example_file_validates() -> None:
    text = (ROOT / "joshua.example.yaml").read_text()
    env = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "BLUEBUBBLES_PASSWORD": "pw",
        "IMESSAGE_WEBHOOK_SECRET": "sekret",
    }
    cfg = config.parse(text, env, source="joshua.example.yaml")
    assert cfg.channels.imessage is not None
    assert "files" in cfg.mcp


def test_unknown_key_names_path() -> None:
    text = GOOD + "\nbogus: 1\n"
    with pytest.raises(config.ConfigError) as exc:
        config.parse(text, ENV, source="test.yaml")
    assert "bogus" in str(exc.value)


def test_household_section_is_rejected() -> None:
    text = (
        "household:\n  name: Home\n  timezone: America/New_York\n"
        "  people:\n    - id: alex\n      name: Alex\n"
    )
    with pytest.raises(config.ConfigError) as exc:
        config.parse(text, ENV, source="test.yaml")
    assert "'household' is not a section" in str(exc.value)
    assert "top level" in str(exc.value)


def test_bad_person_id_names_path() -> None:
    text = GOOD.replace("id: alex", "id: Alex")
    with pytest.raises(config.ConfigError) as exc:
        config.parse(text, ENV, source="test.yaml")
    message = str(exc.value)
    assert "people.0.id" in message


def test_duplicate_handle_fails() -> None:
    text = GOOD.replace('telegram: "112233"', 'telegram: "998877"')
    with pytest.raises(config.ConfigError) as exc:
        config.parse(text, ENV, source="test.yaml")
    assert "duplicate handle telegram:998877" in str(exc.value)


def test_literal_credential_fails_with_path() -> None:
    # A fake token with a real prefix, so the loader must refuse it.
    fake = "glpat-abc123def"  # gitleaks:allow
    text = GOOD.replace("bot_token: ${TELEGRAM_BOT_TOKEN}", f"bot_token: {fake}")
    with pytest.raises(config.ConfigError) as exc:
        config.parse(text, ENV, source="test.yaml")
    message = str(exc.value)
    assert "channels.telegram.bot_token" in message
    assert "belong in .env" in message


def test_credential_from_env_is_allowed() -> None:
    cfg = config.parse(GOOD, {"TELEGRAM_BOT_TOKEN": "glpat-secret-from-env"}, source="test.yaml")
    assert cfg.channels.telegram is not None
    assert cfg.channels.telegram.bot_token == "glpat-secret-from-env"


def test_unset_variable_names_variable() -> None:
    with pytest.raises(config.ConfigError) as exc:
        config.parse(GOOD, {}, source="test.yaml")
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value)


def test_expansion_with_default() -> None:
    text = GOOD.replace("${TELEGRAM_BOT_TOKEN}", "${TELEGRAM_BOT_TOKEN:-fallback}")
    cfg = config.parse(text, {}, source="test.yaml")
    assert cfg.channels.telegram is not None
    assert cfg.channels.telegram.bot_token == "fallback"


def test_people_by_handle_telegram_and_imessage() -> None:
    cfg = config.parse(GOOD, ENV, source="test.yaml")
    assert cfg.people_by_handle("telegram", "998877").id == "alex"
    assert cfg.people_by_handle("imessage", "+15551234567").id == "alex"
    assert cfg.people_by_handle("telegram", "000000") is None


def test_person_and_destination() -> None:
    cfg = config.parse(GOOD, ENV, source="test.yaml")
    assert cfg.person("mia").role == "guest"
    assert cfg.person("nobody") is None
    assert cfg.destination("everyone") == "imessage:group:everyone"
    with pytest.raises(KeyError):
        cfg.destination("missing")


def test_group_lookup() -> None:
    cfg = config.parse(GOOD, ENV, source="test.yaml")
    group = cfg.groups_by_chat("imessage", "iMessage;+;chat100000000000000001")
    assert group is not None
    assert group.id == "everyone"
    assert cfg.groups_by_chat("imessage", "nope") is None


def test_bad_imessage_handle_fails() -> None:
    text = GOOD.replace('imessage: "+15551234567"', 'imessage: "not a handle"')
    with pytest.raises(config.ConfigError) as exc:
        config.parse(text, ENV, source="test.yaml")
    assert "imessage" in str(exc.value)


def test_load_reads_and_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "joshua.yaml"
    path.write_text(GOOD)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv(config.CONFIG_ENV_VAR, str(path))
    config.reload(path)
    first = config.load()
    assert first.name == "Home"
    # A second load returns the cached object even after the file changes.
    path.write_text(GOOD.replace("Home", "Lake house"))
    assert config.load() is first
    assert config.reload().name == "Lake house"


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(config.ConfigError) as exc:
        config.reload(tmp_path / "absent.yaml")
    assert "not found" in str(exc.value)


def test_module_entrypoint_runs_with_no_warning(tmp_path: Path) -> None:
    """``python -m joshua_shared.config`` must not import ``config`` through the
    package first. A RuntimeWarning from runpy is an error here."""
    file = tmp_path / "joshua.yaml"
    file.write_text("name: Home\ntimezone: UTC\npeople:\n  - id: sam\n    name: Sam\n")
    for argv in (["schema"], ["validate", str(file)]):
        result = subprocess.run(
            [sys.executable, "-W", "error::RuntimeWarning", "-m", "joshua_shared.config", *argv],
            capture_output=True,
            text=True,
            env={"PATH": "", "PYTHONPATH": ":".join(sys.path)},
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""


def test_example_channel_secrets_are_optional() -> None:
    """Only the container that owns a secret defines the variable. The example
    file must load in a container with none of the channel credentials set."""
    text = (ROOT / "joshua.example.yaml").read_text()
    cfg = config.parse(text, {}, source="joshua.example.yaml")
    assert cfg.channels.telegram.bot_token == ""
    assert cfg.channels.imessage.bluebubbles_password == ""
    assert cfg.channels.imessage.webhook_path_secret == ""
