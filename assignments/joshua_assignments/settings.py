"""The settings of the assignments container that come from the environment.

``joshua.yaml`` holds the ``assignments`` section. The environment holds the
secrets and the addresses:

- ``DATABASE_URL``: the shared Postgres. Required.
- ``ASSIGNMENTS_TOKENS``: ``person=token,person=token``. Each bearer names
  one person, who must be in ``people``.
- ``ADDON_TOKEN``: one bearer that names no person. It may use only the read
  tools.
- ``CHANNELS_URL`` and ``JOSHUA_TOKEN_ASSIGNMENTS``: where the report and the
  question events go, and the fleet token that sends them. Set both, or
  neither.

Every error message names the variable that is wrong, and never a token.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from joshua_shared.config import JoshuaConfig

_PERSON_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}\Z")


class SettingsError(ValueError):
    """A variable is not valid. The message never contains a token."""


@dataclass(frozen=True)
class Caller:
    """One caller. ``person`` is None for ``ADDON_TOKEN``."""

    person: str | None


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    # Bearer token to caller. Never empty: the container needs a bearer on
    # every route except the probes.
    tokens: Mapping[str, Caller] = field(default_factory=dict, repr=False)
    channels_url: str | None = None
    channels_token: str = field(default="", repr=False)

    @property
    def will_notify(self) -> bool:
        """True when a report or a question goes to channels."""
        return bool(self.channels_url and self.channels_token)

    @property
    def people(self) -> list[str]:
        """The people that a bearer names, sorted."""
        return sorted({c.person for c in self.tokens.values() if c.person})


def _split(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def parse_tokens(raw: str) -> dict[str, str]:
    """Parse ``ASSIGNMENTS_TOKENS`` (``person=token,person=token``) to person to token."""
    result: dict[str, str] = {}
    for item in _split(raw):
        name, sep, token = item.partition("=")
        name = name.strip()
        token = token.strip()
        if not sep or not token:
            raise SettingsError(f"ASSIGNMENTS_TOKENS: the entry for {name!r} has no token")
        if not _PERSON_RE.match(name):
            raise SettingsError(f"ASSIGNMENTS_TOKENS: {name!r} is not a valid person id")
        if name in result:
            raise SettingsError(f"ASSIGNMENTS_TOKENS: the person {name!r} is named twice")
        result[name] = token
    return result


def settings_from_env(cfg: JoshuaConfig, env: Mapping[str, str] | None = None) -> Settings:
    """Build the settings from ``env`` (default ``os.environ``), checked against ``cfg``."""
    env = os.environ if env is None else env

    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SettingsError("DATABASE_URL is required")

    named = parse_tokens(env.get("ASSIGNMENTS_TOKENS", ""))
    known = {person.id for person in cfg.people}
    unknown = sorted(set(named) - known)
    if unknown:
        raise SettingsError(
            f"ASSIGNMENTS_TOKENS names a person that is not in people: {', '.join(unknown)}"
        )
    tokens: dict[str, Caller] = {}
    for person, token in named.items():
        if token in tokens:
            raise SettingsError(
                f"ASSIGNMENTS_TOKENS: {person!r} has the same token as another person"
            )
        tokens[token] = Caller(person=person)
    addon_token = env.get("ADDON_TOKEN", "").strip()
    if addon_token:
        if addon_token in tokens:
            raise SettingsError("ADDON_TOKEN: it is the same as a token in ASSIGNMENTS_TOKENS")
        tokens[addon_token] = Caller(person=None)
    if not tokens:
        raise SettingsError("set ASSIGNMENTS_TOKENS or ADDON_TOKEN: every caller needs a bearer")

    channels_url = env.get("CHANNELS_URL", "").strip() or None
    channels_token = env.get("JOSHUA_TOKEN_ASSIGNMENTS", "").strip()
    if channels_url and not channels_url.startswith(("http://", "https://")):
        raise SettingsError("CHANNELS_URL must start with http:// or https://")
    if bool(channels_url) != bool(channels_token):
        raise SettingsError("CHANNELS_URL and JOSHUA_TOKEN_ASSIGNMENTS must be set together")

    return Settings(
        database_url=database_url,
        tokens=tokens,
        channels_url=channels_url,
        channels_token=channels_token,
    )
