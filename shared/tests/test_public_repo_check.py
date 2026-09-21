"""The guard that keeps one installation's hostnames out of a public repository."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SPEC = importlib.util.spec_from_file_location(
    "check_public_repo", ROOT / "scripts" / "check_public_repo.py"
)
assert SPEC and SPEC.loader
check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check)


def test_a_real_looking_host_is_refused() -> None:
    """The case this exists for: somebody's own machine, named in a doc."""
    assert not check.is_allowed("hub.private-house.net")
    assert not check.is_allowed("wiki.somecompany.co.uk")
    assert not check.is_allowed("52.10.20.30")


def test_a_documentation_address_is_allowed() -> None:
    """RFC 5737 keeps these for writing about networks. They name nobody."""
    for host in ("192.0.2.1", "198.51.100.7", "203.0.113.9"):
        assert check.is_allowed(host), host


def test_a_reserved_name_is_allowed() -> None:
    for host in (
        "example.com",
        "example.net",
        "hub.example.net",
        "a.b.example.org",
        "thing.example",
        "box.test",
        "printer.local",
        "api.internal",
        "svc.cluster.local",
        "localhost",
    ):
        assert check.is_allowed(host), host


def test_a_name_with_no_dot_is_allowed() -> None:
    """A container name resolves in one network and names nobody's machine."""
    for host in ("core", "gateway", "channels", "testserver"):
        assert check.is_allowed(host)


def test_a_private_address_is_allowed_because_the_block_list_needs_it() -> None:
    for host in ("10.0.0.5", "192.168.1.1", "127.0.0.1", "169.254.169.254"):
        assert check.is_allowed(host)


def test_an_upstream_on_the_list_is_allowed_and_says_why() -> None:
    assert check.is_allowed("github.com")
    assert all(reason for reason in check.ALLOWED_HOSTS.values())


def test_a_placeholder_is_not_a_host() -> None:
    assert check.is_allowed("{host}")
    assert check.is_allowed("$SERVER")


def test_the_repository_is_clean_right_now() -> None:
    """The check runs in lint, so this catches a leak before it is pushed."""
    assert check.main() == 0
