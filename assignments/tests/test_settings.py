"""The settings from the environment: the bearers, the database, and channels."""

from __future__ import annotations

import pytest
from conftest import make_config
from joshua_assignments.settings import Caller, SettingsError, parse_tokens, settings_from_env

BASE_ENV = {"DATABASE_URL": "postgresql://joshua:pw@postgres:5432/joshua"}


def env(**extra: str) -> dict[str, str]:
    return {**BASE_ENV, "ASSIGNMENTS_TOKENS": "alex=a-token", **extra}


def test_a_full_environment() -> None:
    settings = settings_from_env(
        make_config(),
        env(
            ASSIGNMENTS_TOKENS="alex=a-token, mia=m-token",
            ADDON_TOKEN="addon",
            CHANNELS_URL="http://channels:8000",
            JOSHUA_TOKEN_ASSIGNMENTS="fleet",
        ),
    )
    assert settings.tokens == {
        "a-token": Caller(person="alex"),
        "m-token": Caller(person="mia"),
        "addon": Caller(person=None),
    }
    assert settings.people == ["alex", "mia"]
    assert settings.will_notify is True
    assert "a-token" not in repr(settings) and "fleet" not in repr(settings)
    assert "postgresql" not in repr(settings)


def test_channels_is_optional() -> None:
    settings = settings_from_env(make_config(), env())
    assert settings.will_notify is False
    assert settings.channels_url is None


def test_the_addon_token_alone_is_enough() -> None:
    settings = settings_from_env(make_config(), {**BASE_ENV, "ADDON_TOKEN": "addon"})
    assert settings.people == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"DATABASE_URL": ""}, "DATABASE_URL is required"),
        ({"ASSIGNMENTS_TOKENS": ""}, "set ASSIGNMENTS_TOKENS or ADDON_TOKEN"),
        ({"ASSIGNMENTS_TOKENS": "bob=b-token"}, "not in people: bob"),
        ({"ASSIGNMENTS_TOKENS": "alex=x,mia=x"}, "the same token as another person"),
        ({"ADDON_TOKEN": "a-token"}, "ADDON_TOKEN: it is the same"),
        ({"CHANNELS_URL": "channels:8000", "JOSHUA_TOKEN_ASSIGNMENTS": "t"}, "must start with"),
        ({"CHANNELS_URL": "http://channels:8000"}, "must be set together"),
        ({"JOSHUA_TOKEN_ASSIGNMENTS": "t"}, "must be set together"),
    ],
)
def test_a_bad_environment_is_refused(changes: dict[str, str], message: str) -> None:
    with pytest.raises(SettingsError) as exc:
        settings_from_env(make_config(), env(**changes))
    assert message in str(exc.value)
    for value in changes.values():
        if "token" in value or value == "x":
            assert f"={value}" not in str(exc.value)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("alex", "has no token"),
        ("alex=", "has no token"),
        ("Alex=t", "not a valid person id"),
        ("alex=t,alex=u", "named twice"),
    ],
)
def test_parse_tokens_refuses(raw: str, message: str) -> None:
    with pytest.raises(SettingsError) as exc:
        parse_tokens(raw)
    assert message in str(exc.value)


def test_parse_tokens_skips_empty_items() -> None:
    assert parse_tokens(" alex=t , ,mia=u ") == {"alex": "t", "mia": "u"}


def test_the_environment_defaults_to_os_environ(monkeypatch) -> None:
    for key, value in env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("CHANNELS_URL", raising=False)
    monkeypatch.delenv("JOSHUA_TOKEN_ASSIGNMENTS", raising=False)
    monkeypatch.delenv("ADDON_TOKEN", raising=False)
    assert settings_from_env(make_config()).people == ["alex"]
