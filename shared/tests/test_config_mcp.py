"""Tests for the ``mcp:`` models in joshua_shared.config."""

from __future__ import annotations

import textwrap

import pytest
from joshua_shared import config

ROSTER = """
name: Home
timezone: America/New_York
people:
  - id: alex
    name: Alex
  - id: mia
    name: Mia
"""


def parse_mcp(mcp_block: str, env: dict[str, str] | None = None) -> config.JoshuaConfig:
    text = ROSTER + "\nmcp:\n" + textwrap.indent(textwrap.dedent(mcp_block), "  ")
    return config.parse(text, env or {}, source="test.yaml")


def test_all_three_shapes_and_view_validate() -> None:
    cfg = parse_mcp(
        """
        files:
          kind: builtin
          allow: all
          options:
            verbose: false
        weather:
          type: stdio
          command: uvx
          args:
            - mcp-weather
          env:
            WEATHER_KEY: ${WEATHER_KEY}
          allow: all
          tools:
            allow:
              - "get_*"
            deny:
              - "*_admin"
            class:
              "get_*": read
          url_args: none
        spotify:
          type: http
          url: https://spotify-mcp.example/mcp
          allow:
            - alex
            - mia
          identities:
            alex:
              headers:
                Authorization: "Bearer ${SPOTIFY_TOKEN_ALEX}"
            mia:
              headers:
                Authorization: "Bearer ${SPOTIFY_TOKEN_MIA}"
        tracker-readonly:
          type: stdio
          command: mcp-tracker
          allow:
            - alex
          tools:
            allow:
              - "get_*"
              - "list_*"
              - "search_*"
        """,
        env={
            "WEATHER_KEY": "wk",
            "SPOTIFY_TOKEN_ALEX": "sj",
            "SPOTIFY_TOKEN_MIA": "sl",
        },
    )
    assert set(cfg.mcp) == {"files", "weather", "spotify", "tracker-readonly"}
    assert cfg.mcp["files"].kind == "builtin"
    assert cfg.mcp["weather"].tools.allow == ["get_*"]
    assert cfg.mcp["weather"].tools.deny == ["*_admin"]
    assert cfg.mcp["weather"].tools.tool_class == {"get_*": "read"}
    assert cfg.mcp["weather"].url_args == "none"
    assert cfg.mcp["weather"].env["WEATHER_KEY"] == "wk"
    assert cfg.mcp["spotify"].allow == ["alex", "mia"]
    assert cfg.mcp["spotify"].identities["alex"].headers["Authorization"] == "Bearer sj"


def test_default_allow_is_all_and_url_args_grant_required() -> None:
    cfg = parse_mcp(
        """
        weather:
          type: stdio
          command: uvx
        """
    )
    assert cfg.mcp["weather"].allow == "all"
    assert cfg.mcp["weather"].url_args == "grant-required"
    assert cfg.mcp["weather"].tools is None


def test_bad_server_name_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            Weather:
              type: stdio
              command: uvx
            """
        )
    assert "must match" in str(exc.value)


def test_builtin_kind_only_for_reserved_names() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            weather:
              kind: builtin
              allow: all
            """
        )
    assert "kind builtin" in str(exc.value)


def test_stdio_requires_command() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            weather:
              type: stdio
            """
        )
    assert "requires 'command'" in str(exc.value)


def test_http_requires_url() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            spotify:
              type: http
              allow: all
            """
        )
    assert "requires 'url'" in str(exc.value)


def test_allow_unknown_person_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            spotify:
              type: http
              url: https://x/mcp
              allow:
                - nobody
            """
        )
    assert "unknown person id 'nobody'" in str(exc.value)


def test_identities_unknown_person_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            spotify:
              type: http
              url: https://x/mcp
              allow:
                - alex
              identities:
                ghost:
                  headers:
                    Authorization: "token"
            """
        )
    assert "unknown person id 'ghost'" in str(exc.value)


def test_allowed_person_without_identity_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            spotify:
              type: http
              url: https://x/mcp
              allow:
                - alex
                - mia
              identities:
                alex:
                  headers:
                    Authorization: "token"
            """
        )
    assert "mia is allowed but has no identity" in str(exc.value)


def test_identities_with_allow_all_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            spotify:
              type: http
              url: https://x/mcp
              allow: all
              identities:
                alex:
                  headers:
                    Authorization: "token"
            """
        )
    assert "explicit allow list" in str(exc.value)


def test_identity_env_on_http_server_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            spotify:
              type: http
              url: https://x/mcp
              allow:
                - alex
              identities:
                alex:
                  env:
                    TOKEN: "x"
            """
        )
    assert "stdio" in str(exc.value)


def test_identity_headers_on_stdio_server_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            tracker:
              type: stdio
              command: mcp-tracker
              allow:
                - alex
              identities:
                alex:
                  headers:
                    Authorization: "token"
            """
        )
    assert "http or sse" in str(exc.value)


def test_identities_on_builtin_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            files:
              kind: builtin
              allow:
                - alex
              identities:
                alex:
                  env:
                    TOKEN: "x"
            """
        )
    assert "builtin" in str(exc.value)


def test_bad_transport_type_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            weather:
              type: carrier-pigeon
              command: uvx
            """
        )
    assert "type must be one of" in str(exc.value)


def test_bad_tool_class_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            weather:
              type: stdio
              command: uvx
              tools:
                class:
                  "get_*": teleport
            """
        )
    assert "tool class" in str(exc.value)


def test_bad_url_args_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            weather:
              type: stdio
              command: uvx
              url_args: maybe
            """
        )
    assert "url_args must be one of" in str(exc.value)


# -- package entries -----------------------------------------------------------


def test_the_four_package_kinds_validate() -> None:
    cfg = parse_mcp(
        """
        weather:
          type: stdio
          package: npm:weather-mcp@1.6.1
          env:
            ENABLED_TOOLS: standard
        ha:
          type: stdio
          package: pypi:ha-mcp==7.8.1
        wikiserver:
          type: stdio
          package: git+https://github.com/example/wiki-mcp@3f2a9c1
          command: python
          args:
            - -m
            - wiki_mcp
        weather-bin:
          type: stdio
          package: https://example.net/dl/weather-mcp-linux-amd64
          sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        """
    )
    assert set(cfg.mcp) == {"weather", "ha", "wikiserver", "weather-bin"}
    assert cfg.mcp["weather"].command is None
    assert cfg.mcp["wikiserver"].command == "python"


def test_a_package_stands_in_for_command() -> None:
    """A `package` entry needs no `command`: the installer resolves the executable."""
    cfg = parse_mcp(
        """
        weather:
          type: stdio
          package: npm:weather-mcp@1.6.1
        """
    )
    assert cfg.mcp["weather"].package == "npm:weather-mcp@1.6.1"


def test_a_stdio_server_with_neither_command_nor_package_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            weather:
              type: stdio
            """
        )
    assert "requires 'command' or 'package'" in str(exc.value)


def test_an_unpinned_package_fails_and_says_how_to_pin() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            weather:
              type: stdio
              package: npm:weather-mcp@latest
            """
        )
    assert "pin an exact version" in str(exc.value)


def test_a_url_package_without_sha256_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            weather:
              type: stdio
              package: https://example.net/dl/weather-mcp
            """
        )
    assert "sha256" in str(exc.value)


def test_a_package_on_an_http_server_fails() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            weather:
              type: http
              url: https://weather.example/mcp
              package: npm:weather-mcp@1.6.1
            """
        )
    assert "only to a stdio server" in str(exc.value)


def test_package_only_fields_need_a_package() -> None:
    for field, value in (("sha256", '"' + "a" * 64 + '"'), ("registry", "https://npm.example")):
        with pytest.raises(config.ConfigError) as exc:
            parse_mcp(
                f"""
                weather:
                  type: stdio
                  command: mcp-weather
                  {field}: {value}
                """
            )
        assert "applies only to a package server" in str(exc.value)


def test_allow_scripts_needs_a_package() -> None:
    with pytest.raises(config.ConfigError) as exc:
        parse_mcp(
            """
            weather:
              type: stdio
              command: mcp-weather
              allow_scripts: true
            """
        )
    assert "applies only to a package server" in str(exc.value)
