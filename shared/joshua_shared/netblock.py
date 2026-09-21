"""The block list that keeps a web fetch off the private network.

`WebFetch` runs in the container, not on Anthropic's side. So a page that
Joshua reads can name a host on your own network, and Joshua would be the one
to fetch it. That is the confused deputy: the agent has no reason to reach your
house, but it sits where it can.

This module decides whether one URL may be fetched. `block_reason` returns the
reason to refuse, or `None` to allow. The rules, in order:

1. A scheme that is not `http` or `https` is refused.
2. A host that is an IP address in a private, loopback, link-local, or
   otherwise reserved range is refused. `169.254.169.254`, the address that
   holds cloud credentials, is in that set.
3. A host that matches an entry of the block list is refused. An entry is a
   CIDR (`10.0.0.0/8`), a domain (`example.net`, which also refuses
   `hub.example.net`), a pattern (`*.example.net`), a host, or plain text to
   look for in the URL.
4. With `resolve` on, the host is looked up, and a name that resolves to a
   private address is refused. This is what stops a public name that points
   inside.

**What this does not stop.** A redirect: the hook sees the URL the model asked
for, and a page that answers with `302` to a private address is followed by the
fetch itself. A name that resolves to a public address at the check and a
private one at the fetch (DNS rebinding) also gets through. Treat the block
list as the fence around a mistake, not as a wall against an attacker who
already runs a server. `docs/security.md` says the same.
"""

from __future__ import annotations

import ipaddress
import socket
from fnmatch import fnmatch
from urllib.parse import urlsplit

# The schemes a fetch may use. Everything else (file, gopher, ftp) is refused.
ALLOWED_SCHEMES = frozenset({"http", "https"})

# The entries a person gets without writing any. Each is a network a fetch has
# no business reaching from inside a home or an office.
DEFAULT_BLOCKED: tuple[str, ...] = (
    "10.0.0.0/8",  # RFC 1918
    "172.16.0.0/12",  # RFC 1918
    "192.168.0.0/16",  # RFC 1918
    "127.0.0.0/8",  # loopback
    "169.254.0.0/16",  # link local, and the cloud metadata address
    "::1/128",  # loopback, IPv6
    "fc00::/7",  # unique local, IPv6
    "fe80::/10",  # link local, IPv6
    "localhost",
    ".local",  # mDNS
    ".internal",
    ".home.arpa",
    ".svc",  # a service inside Kubernetes
    ".cluster.local",
)


def _host_of(url: str) -> str:
    """The host of `url`, in lower case, with no port and no brackets."""
    host = urlsplit(url.strip()).hostname or ""
    return host.lower().strip(".")


def _as_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for an address that belongs to a network, not to the open web."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


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
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
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
    blocked: list[str] | tuple[str, ...] = DEFAULT_BLOCKED,
    *,
    resolve: bool = True,
) -> str | None:
    """Why `url` may not be fetched, or `None` when it may.

    The reason names the rule and never the whole URL, so a log line holds no
    page a person visited.
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

    ip = _as_ip(host)
    if ip is not None and _is_private(ip):
        return "the URL names an address on a private network"

    for entry in blocked:
        if _matches(entry, raw, host):
            return f"the host is covered by the blocked entry {entry!r}"

    if resolve and ip is None:
        for found in _resolved(host):
            if _is_private(found):
                return "the host resolves to an address on a private network"

    return None
