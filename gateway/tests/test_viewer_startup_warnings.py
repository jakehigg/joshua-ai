"""What the viewer logs at startup about a setting that cannot work.

``viewer.startup_warnings`` is one pure function over the config and the
environment, so each line is checked here without starting the server. A line
names the setting and says what to set, and no line ever holds a secret.
"""

from __future__ import annotations

import textwrap

from joshua_gateway import viewer, viewer_session
from joshua_shared import config

PEOPLE = textwrap.dedent("""\
    name: Test
    timezone: America/New_York
    people:
      - id: alex
        name: Alex
        role: owner
      - id: mia
        name: Mia
        role: guest
    """)

SECRET = viewer_session.SESSION_SECRET_ENV

# An environment with nothing wrong in it: the session key is set.
ENV = {SECRET: "a-random-key"}


def _cfg(viewer_block: str) -> config.JoshuaConfig:
    return config.parse(PEOPLE + viewer_block, {}, source="test.yaml")


def _messages(cfg: config.JoshuaConfig, env: dict[str, str]) -> list[str]:
    return [str(line["message"]) for line in viewer.startup_warnings(cfg, env)]


def test_a_viewer_nobody_can_read_is_logged() -> None:
    """The default of a fresh install: the viewer asks for no secret and is
    not open either, so every page refuses every request."""
    lines = viewer.startup_warnings(_cfg("viewer:\n  enabled: true\n"), ENV)
    assert [line["message"] for line in lines] == [config.NO_VIEWER_ACCESS_WARNING]
    assert lines[0]["setting"] == "viewer.anonymous"
    # The two ways out of it say nothing.
    assert _messages(_cfg("viewer:\n  enabled: true\n  anonymous: true\n"), ENV) == []
    assert _messages(_cfg("viewer:\n  enabled: true\n  auth:\n    method: pin\n"), ENV) == []


def test_a_missing_session_key_is_logged() -> None:
    """Without the variable the viewer mints a key of its own, so every
    restart signs everybody out. The line names the variable, never a key."""
    open_viewer = _cfg("viewer:\n  enabled: true\n  anonymous: true\n")
    lines = viewer.startup_warnings(open_viewer, {})
    assert len(lines) == 1
    first = lines[0]
    assert first["setting"] == SECRET
    assert SECRET in str(first["message"])
    assert "restart" in str(first["message"])
    # A blank value is not a key either.
    assert _messages(open_viewer, {SECRET: "  "})[0] == first["message"]
    assert SECRET not in [line["setting"] for line in viewer.startup_warnings(open_viewer, ENV)]
    assert "a-random-key" not in str(viewer.startup_warnings(open_viewer, ENV))


def test_an_anonymous_person_id_is_logged() -> None:
    """``viewer.anonymous: <id>`` serves every request as that person, with
    their role, so the line names the setting and the person it grants."""
    lines = viewer.startup_warnings(_cfg("viewer:\n  enabled: true\n  anonymous: alex\n"), ENV)
    assert len(lines) == 1
    assert lines[0]["setting"] == "viewer.anonymous"
    assert lines[0]["person"] == "alex"
    assert "role" in str(lines[0]["message"])


def test_the_anonymous_person_fix_names_the_method_when_it_must() -> None:
    """Under the anonymous method, ``viewer.anonymous: false`` alone gives a
    viewer nobody can read, so the line also says to set a method that asks
    for a secret. Under pin it says only to turn the setting off."""
    anon = _messages(_cfg("viewer:\n  enabled: true\n  anonymous: alex\n"), ENV)
    assert "viewer.auth.method: pin (or password) and viewer.anonymous: false" in anon[0]
    pin = _messages(
        _cfg("viewer:\n  enabled: true\n  anonymous: alex\n  auth:\n    method: pin\n"), ENV
    )
    assert "viewer.auth.method" not in pin[0]
    assert "set viewer.anonymous: false to ask for a sign-in" in pin[0]


def test_a_viewer_with_a_sign_in_and_a_key_says_nothing() -> None:
    assert _messages(_cfg("viewer:\n  enabled: true\n  auth:\n    method: pin\n"), ENV) == []
