"""The ``package:`` grammar: every kind parses, and nothing unpinned gets through."""

from __future__ import annotations

import pytest
from joshua_shared.mcp_package import PackageError, default_command, parse_package


def test_npm_with_an_exact_version():
    spec = parse_package("npm:weather-mcp@1.6.1")
    assert (spec.kind, spec.name, spec.version) == ("npm", "weather-mcp", "1.6.1")
    assert spec.requirement == "weather-mcp@1.6.1"
    assert spec.bare_name == "weather-mcp"


def test_npm_keeps_the_scope_and_reports_the_bare_name():
    spec = parse_package("npm:@zereight/mcp-gitlab@1.0.83")
    assert spec.name == "@zereight/mcp-gitlab"
    assert spec.version == "1.0.83"
    assert spec.bare_name == "mcp-gitlab"


def test_npm_takes_a_prerelease():
    assert parse_package("npm:thing@2.0.0-rc.1").version == "2.0.0-rc.1"


def test_pypi_with_a_double_equals():
    spec = parse_package("pypi:ha-mcp==7.8.1")
    assert (spec.kind, spec.name, spec.version) == ("pypi", "ha-mcp", "7.8.1")
    assert spec.requirement == "ha-mcp==7.8.1"


def test_git_at_a_commit():
    spec = parse_package("git+https://github.com/example/wiki-mcp@3f2a9c1")
    assert spec.kind == "git"
    assert spec.name == "https://github.com/example/wiki-mcp"
    assert spec.version == "3f2a9c1"


def test_git_at_a_release_tag():
    assert parse_package("git+https://example.net/a/b@v1.2.0").version == "v1.2.0"


def test_url_with_a_hash():
    digest = "a" * 64
    spec = parse_package("https://example.net/dl/weather-mcp-linux-amd64", digest)
    assert spec.kind == "url"
    assert spec.sha256 == digest
    assert default_command(spec) == "weather-mcp-linux-amd64"


def test_a_package_that_is_not_a_url_has_no_default_command():
    assert default_command(parse_package("npm:weather-mcp@1.6.1")) is None


# -- what must not get through -------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "npm:weather-mcp@latest",
        "npm:weather-mcp@^1.6.1",
        "npm:weather-mcp@~1.6",
        "npm:weather-mcp@1.x",
        "npm:weather-mcp",
        "pypi:ha-mcp>=7.8.1",
        "pypi:ha-mcp",
        "pypi:ha-mcp==7.8.*",
        "git+https://github.com/example/wiki-mcp@main",
        "git+https://github.com/example/wiki-mcp@develop",
        "git+https://github.com/example/wiki-mcp",
        "weather-mcp",
        "",
    ],
)
def test_an_unpinned_spec_is_refused_and_the_message_says_how_to_pin(raw):
    with pytest.raises(PackageError) as exc:
        parse_package(raw)
    assert "npm:<name>@1.2.3" in str(exc.value) or "must name one of" in str(exc.value)


def test_a_plain_http_download_is_refused():
    with pytest.raises(PackageError, match="https"):
        parse_package("http://example.net/dl/thing", "a" * 64)


def test_a_url_package_needs_a_hash():
    with pytest.raises(PackageError, match="sha256"):
        parse_package("https://example.net/dl/thing")


def test_a_url_hash_must_be_a_sha256():
    with pytest.raises(PackageError, match="64 hexadecimal"):
        parse_package("https://example.net/dl/thing", "not-a-hash")


def test_a_url_package_must_name_a_file():
    with pytest.raises(PackageError, match="name a file"):
        parse_package("https://example.net/dl/", "a" * 64)


def test_a_hash_on_a_registry_package_is_refused():
    """A version pin and a file hash are two ways to say the same thing, and only
    one of them applies to a registry package."""
    with pytest.raises(PackageError, match="applies only to an https"):
        parse_package("npm:weather-mcp@1.6.1", "a" * 64)


def test_a_git_package_must_use_https():
    with pytest.raises(PackageError, match="git\\+https"):
        parse_package("git+ssh://git@github.com/example/wiki-mcp@3f2a9c1")
