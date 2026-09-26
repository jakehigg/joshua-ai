"""Refuse a hostname that belongs to one installation.

This repository is public. The people who run Joshua are not, and neither are
their networks. A hostname of a real deployment in a doc, a comment, an
example, or a test tells a reader what to aim at, and it is impossible to take
back once it is pushed.

So every host inside a URL in a tracked file must be one of:

* a name reserved for documentation: `example.com`, `example.net`,
  `example.org` and anything under them, or anything under `.example`,
  `.test`, `.invalid`, or `.localhost` (RFC 2606 and RFC 6761);
* a name that resolves nowhere real: `localhost`, `.local`, `.internal`,
  `.home.arpa`, a Kubernetes service name, or a host with no dot in it (a
  container name such as `core` or `gateway`);
* an address on a private, loopback, or carrier-grade NAT network, which the
  block list and its tests must name to do their job;
* a host that is not a name DNS can hold, or an address written in another
  notation (`0x7f.1`, `127.1`), which the tests of the block list use to prove
  that a URL the fetch reads differently is refused;
* a public project this repository genuinely depends on, listed in
  `ALLOWED_HOSTS` below, with the reason it is there.

Anything else fails. Use a reserved name instead. If a new upstream truly
belongs here, add it to `ALLOWED_HOSTS` in the same change, where a reviewer
sees it.

Run it with `make lint`, or directly:

    uv run python scripts/check_public_repo.py
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent

# The file kinds a person reads or copies from.
PATTERNS = ("*.md", "*.py", "*.yaml", "*.yml", "*.toml", "*.sh", ".env.example")

URL = re.compile(r"https?://[^\s\"'`<>)\]},]+", re.I)

# Suffixes that name no real place.
RESERVED_SUFFIXES = (
    # RFC 2606 reserves these three names and everything under them.
    ".example.com",
    ".example.net",
    ".example.org",
    ".example",
    ".test",
    ".invalid",
    ".localhost",
    ".local",
    ".internal",
    ".home.arpa",
    ".svc",
    ".cluster.local",
)

RESERVED_NAMES = frozenset({"localhost", "example.com", "example.net", "example.org"})

# A public project this repository depends on or documents. Each line says why,
# so that a reader can tell a dependency from a leak.
ALLOWED_HOSTS = {
    "github.com": "the repository, the releases, and the workflows",
    "raw.githubusercontent.com": "a file fetched from a repository",
    "ghcr.io": "the container registry the images ship from",
    "docs.astral.sh": "the uv documentation",
    "pypi.org": "the Python package index",
    "files.pythonhosted.org": "the package files themselves",
    "registry.npmjs.org": "the npm registry a package server installs from",
    "www.npmjs.com": "an npm package page",
    "docs.anthropic.com": "the API and Agent SDK documentation",
    "code.claude.com": "the Claude Code documentation",
    "claude.ai": "the session link in a commit trailer",
    "en.wikipedia.org": "a reference in prose",
    "asd-ste100.org": "the writing standard the docs follow",
    "usememos.com": "the notes service the memos source reads",
    "otterwiki.com": "the wiki frontend shipped as an enhancement",
    "bluebubbles.app": "the iMessage bridge the channel talks to",
    "localtest.me": "a name that resolves to 127.0.0.1, for a block list test",
    "www.seriouseats.com": "a sample result in an internet agent test",
    "8.8.8.8": "a public resolver, so a test can prove a public address is allowed",
}


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--", *PATTERNS],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [ROOT / line for line in result.stdout.split() if line]


# What a host name may hold, and an IPv4 address in the usual notation.
DNS_NAME = re.compile(r"[a-z0-9_.-]+")
DOTTED_QUAD = re.compile(r"\d{1,3}(\.\d{1,3}){3}")

# Shared address space (RFC 6598), where a Tailscale address lives. Python does
# not call it private, and the block list must name it.
CARRIER_GRADE_NAT = ipaddress.ip_network("100.64.0.0/10")


def host_of(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def is_allowed(host: str) -> bool:
    """True when a host names nowhere real, or a project this repo depends on."""
    if "{" in host or "}" in host or "$" in host:
        return True  # a placeholder in an f-string or a template
    if host in RESERVED_NAMES or host in ALLOWED_HOSTS:
        return True
    if host.endswith(RESERVED_SUFFIXES):
        return True
    if "." not in host:
        return True  # a container or a service name, which resolves in one network
    host = host.rstrip(".")
    if not DNS_NAME.fullmatch(host):
        return True  # not a name DNS can hold: a test of a URL the fetch misreads
    last = host.rsplit(".", 1)[-1]
    if (last.isdigit() or last.startswith("0x")) and not DOTTED_QUAD.fullmatch(host):
        return True  # an address in another notation, which names no host
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return last.isdigit()  # four numbers that are not an address name nowhere
    # The block list and its tests must name these to prove what they refuse.
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip in CARRIER_GRADE_NAT
    )


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        if path.name == Path(__file__).name:
            continue  # this file lists the allowed hosts on purpose
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, start=1):
            for url in URL.findall(line):
                host = host_of(url)
                if host and not is_allowed(host):
                    rel = path.relative_to(ROOT)
                    findings.append(f"{rel}:{number}: {host}")

    if findings:
        print("public-repo: a host that is not reserved and not an allowed upstream:")
        for finding in sorted(set(findings)):
            print(f"  {finding}")
        print()
        print("This repository is public. Use a reserved name (example.com,")
        print("example.net, anything under .example or .test), or add the host to")
        print("ALLOWED_HOSTS in scripts/check_public_repo.py with the reason.")
        return 1

    print("public-repo: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
