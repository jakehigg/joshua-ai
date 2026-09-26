"""Decide whether one URL may be fetched, from the operator's block list.

`WebFetch` runs in the container, not on Anthropic's side. So a page that
Joshua reads can name a host on the operator's own network, and Joshua would be
the one to fetch it. `research.fetch.blocked` in `joshua.yaml` says what a
fetch may never reach.

**The list is the whole policy.** Nothing is blocked here that the list does
not name, and an empty list blocks nothing at all. That is deliberate: a person
who wants Joshua to read a page on their own network may have it, and the
shipped `joshua.example.yaml` carries a list that a careful person would start
from. It is theirs to edit.

An entry is one of:

* a CIDR, `10.0.0.0/8`, matched against the address of the host, and against
  every address the host resolves to when `resolve_hosts` is on;
* a domain, `example.net`, which covers the host and every host under it;
* a pattern, `*.example.net` or `hub.*`, matched against the host;
* a host with no dot, `localhost`;
* plain text, looked for anywhere in the URL.

One rule is not the list's: a scheme that is not `http` or `https` is always
refused. That is about the protocol, not about a place, and no entry can turn
`file://` into something a fetch may open.

**What a block list cannot do.** A redirect: this reads the URL the model
asked for, and a page that answers `302` to an address inside is followed by
the fetch itself. A name that resolves to a public address at the check and a
private one at the fetch gets through the same way. Treat the list as the fence
around a mistake, not as a wall against somebody who already runs a server and
aims it at you. A person who needs that wall gives the container egress to the
open web alone, which the network decides and this code cannot.
"""

from __future__ import annotations

import ipaddress
import socket
from fnmatch import fnmatch
from urllib.parse import urlsplit

# The schemes a fetch may use. Everything else (file, gopher, ftp) is refused.
ALLOWED_SCHEMES = frozenset({"http", "https"})


def _host_of(url: str) -> str:
    """The host of `url`, in lower case, with no port and no brackets."""
    host = urlsplit(url.strip()).hostname or ""
    return host.lower().strip(".")


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _resolved(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Every address `host` resolves to. An empty list when it resolves to none."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    found = []
    for info in infos:
        address = info[4][0]
        ip = _as_ip(str(address))
        if ip is not None:
            found.append(ip)
    return found


def _network(entry: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """The network an entry names, or `None` when it names something else."""
    entry = entry.strip().lower()
    if "/" not in entry:
        return None
    try:
        return ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return None


def _matches(entry: str, url: str, host: str) -> bool:
    """True when one block list entry covers this URL.

    An entry that holds `*` or `?` is a pattern, matched against the host:
    `*.example.net` covers `hub.example.net` and `example.net` itself, and
    `hub.*` covers that host on any domain. A CIDR is matched against the host
    as an address. An entry that holds a dot is a domain, matched against the
    host and every host under it, so `example.net` covers `hub.example.net` and
    refuses nothing else. An entry with no dot is matched against the host and
    then looked for in the whole URL, which is the plain-text case.
    """
    entry = entry.strip().lower()
    if not entry:
        return False

    if "*" in entry or "?" in entry:
        # A person writes `*.example.net` and means the house. It covers the
        # hosts under the domain and the domain itself, because a block list
        # that quietly misses the apex is worse than one that says no twice.
        if entry.startswith("*.") and (host == entry[2:] or host.endswith(entry[1:])):
            return True
        return fnmatch(host, entry)

    if "/" in entry:
        network = _network(entry)
        if network is None:
            return entry in url.lower()
        ip = _as_ip(host)
        return ip is not None and ip.version == network.version and ip in network

    if entry.startswith("."):
        return host == entry[1:] or host.endswith(entry)

    if _as_ip(entry) is not None:
        return host == entry

    if host == entry or host.endswith(f".{entry}"):
        return True

    # An entry that holds a dot is a domain, and a domain is matched against the
    # host alone. Without this, `example.net` would also refuse
    # `example.net.other.example`, which is somebody else's host.
    if "." in entry:
        return False

    # A plain word is looked for in the whole URL. This is the simple case: the
    # text a person does not want fetched, wherever it appears.
    return entry in url.lower()


def block_reason(
    url: str,
    blocked: list[str] | tuple[str, ...],
    *,
    resolve: bool = True,
) -> str | None:
    """Why `url` may not be fetched, or `None` when it may.

    `blocked` is the operator's list and it is the whole policy: an empty list
    refuses nothing but a scheme that is not `http` or `https`. The reason names
    the rule and never the whole URL, so a log line holds no page a person read.
    """
    raw = (url or "").strip()
    if not raw:
        return "the tool asked for no URL"

    parts = urlsplit(raw)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return f"the scheme {parts.scheme or 'none'!r} is not http or https"

    host = _host_of(raw)
    if not host:
        return "the URL names no host"

    for entry in blocked:
        if _matches(entry, raw, host):
            return f"the host is covered by the blocked entry {entry!r}"

    if resolve and _as_ip(host) is None:
        networks = [n for n in (_network(e) for e in blocked) if n is not None]
        if networks:
            for found in _resolved(host):
                for network in networks:
                    if found.version == network.version and found in network:
                        return f"the host resolves into the blocked entry {str(network)!r}"

    return None
