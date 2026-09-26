"""Decide whether one URL may be fetched, from the operator's block list.

`WebFetch` runs in the container, not on Anthropic's side. So a page that
Joshua reads can name a host on the operator's own network, and Joshua would be
the one to fetch it. `internet.fetch.blocked` in `joshua.yaml` says what a
fetch may never reach.

**The list is the whole policy for a place.** Nothing is blocked here that the
list does not name, and an empty list blocks nothing at all. That is
deliberate: a person who wants Joshua to read a page on their own network may
have it, and the shipped `joshua.example.yaml` carries a list that a careful
person would start from. It is theirs to edit.

An entry is one of:

* a CIDR, `10.0.0.0/8`, matched against the address of the host, and against
  every address the host resolves to when `resolve_hosts` is on;
* a domain, `example.net`, which covers the host and every host under it;
* a pattern, `*.example.net` or `hub.*`, matched against the host;
* a host with no dot, `localhost`;
* plain text, looked for anywhere in the URL.

**Some rules are not the list's.** They are about reading the URL, not about a
place, and no entry turns them off:

* A scheme that is not `http` or `https` is refused.
* A URL is refused when this check and the fetch could read two different
  hosts from it. The fetch runs in Node, which reads a URL by the WHATWG rules,
  and Python does not. So a backslash, a user name or a password, a `%` in the
  host, a space or a control character, or a host that is not a valid name is
  refused, never guessed at.
* A host that Node reads as an IPv4 address is read the same way here, in each
  form Node accepts: `2130706433`, `0x7f.1`, `0177.0.0.1`, `127.1`.
* An IPv6 address that carries an IPv4 address (`::ffff:127.0.0.1`, the NAT64
  prefix, 6to4) is checked as that IPv4 address too.

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
import re
import socket
from fnmatch import fnmatch
from urllib.parse import urlsplit

# The schemes a fetch may use. Everything else (file, gopher, ftp) is refused.
ALLOWED_SCHEMES = frozenset({"http", "https"})

type Address = ipaddress.IPv4Address | ipaddress.IPv6Address

# A backslash, a space, and every control character. WHATWG reads a backslash
# as a slash and drops a tab or a newline inside a URL, and Python does
# neither, so the two could see two hosts.
_AMBIGUOUS = re.compile(r"[\\\s\x00-\x1f\x7f]")

# A host name as DNS spells it, after IDNA: letters, digits, hyphens, dots.
_HOST_NAME = re.compile(r"^[a-z0-9_-]+(\.[a-z0-9_-]+)*$")

# The well-known NAT64 prefix. An address under it carries an IPv4 address in
# its last 32 bits, and a NAT64 gateway connects to that address.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class UnreadableUrl(ValueError):
    """A URL this check cannot read as the fetch would."""


def _parse_number(part: str) -> int:
    """One part of a WHATWG IPv4 address: decimal, `0x` hex, or `0` octal."""
    if part[:2] in ("0x", "0X"):
        digits = part[2:]
        return int(digits, 16) if digits else 0
    if len(part) > 1 and part.startswith("0"):
        return int(part[1:], 8)
    return int(part, 10)


def _ends_in_number(host: str) -> bool:
    """True when WHATWG reads `host` as an IPv4 address, valid or not."""
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts = parts[:-1]
    last = parts[-1]
    if last and last.isdigit():
        return True
    return last[:2] in ("0x", "0X") and all(c in "0123456789abcdefABCDEF" for c in last[2:])


def _whatwg_ipv4(host: str) -> ipaddress.IPv4Address:
    """Read `host` as WHATWG reads an IPv4 address. Raises `UnreadableUrl`."""
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts = parts[:-1]
    if len(parts) > 4 or any(p == "" for p in parts):
        raise UnreadableUrl("the host is not a valid address")
    try:
        numbers = [_parse_number(p) for p in parts]
    except ValueError as exc:
        raise UnreadableUrl("the host is not a valid address") from exc
    if any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        raise UnreadableUrl("the host is not a valid address")
    value = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        value += number * 256 ** (3 - index)
    return ipaddress.IPv4Address(value)


def _host_of(url: str) -> tuple[str, Address | None]:
    """The host of `url` as the fetch reads it, and its address when it is one.

    Raises `UnreadableUrl` for a URL that this check and the fetch could read
    as two different hosts.
    """
    if _AMBIGUOUS.search(url):
        raise UnreadableUrl("the URL holds a backslash, a space, or a control character")

    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        raise UnreadableUrl("the URL holds a user name or a password")
    if "%" in netloc:
        raise UnreadableUrl("the host holds a percent sign")
    try:
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError as exc:
        raise UnreadableUrl("the port is not a number") from exc

    raw_host = parts.hostname or ""
    if not raw_host:
        raise UnreadableUrl("the URL names no host")

    if netloc.startswith("["):
        try:
            return raw_host.lower(), ipaddress.IPv6Address(raw_host)
        except ValueError as exc:
            raise UnreadableUrl("the host is not a valid address") from exc

    # IDNA folds a name as WHATWG does, so a full-width digit or a capital
    # becomes the plain character the fetch would connect to.
    try:
        host = raw_host.encode("idna").decode("ascii").lower() if raw_host else ""
    except UnicodeError as exc:
        raise UnreadableUrl("the host is not a valid name") from exc
    host = host.rstrip(".")
    if not host:
        raise UnreadableUrl("the URL names no host")

    if _ends_in_number(host):
        address = _whatwg_ipv4(host)
        return str(address), address
    if not _HOST_NAME.match(host):
        raise UnreadableUrl("the host is not a valid name")
    return host, None


def _embedded(address: Address) -> list[Address]:
    """`address`, and the IPv4 address it carries when it is an IPv6 wrapper."""
    found: list[Address] = [address]
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            found.append(address.ipv4_mapped)
        elif address.sixtofour is not None:
            found.append(address.sixtofour)
        elif address in _NAT64:
            found.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
        elif int(address) >> 32 == 0 and int(address) > 1:
            # The deprecated IPv4-compatible form, `::127.0.0.1`.
            found.append(ipaddress.IPv4Address(int(address)))
    return found


def _resolved(host: str) -> list[Address]:
    """Every address `host` resolves to. An empty list when it resolves to none."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    found: list[Address] = []
    for info in infos:
        try:
            found.append(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]))
        except ValueError:
            continue
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


def _in_network(
    addresses: list[Address], network: ipaddress.IPv4Network | ipaddress.IPv6Network
) -> bool:
    return any(a.version == network.version and a in network for a in addresses)


def _matches(entry: str, url: str, host: str, addresses: list[Address]) -> bool:
    """True when one block list entry covers this URL.

    An entry that holds `*` or `?` is a pattern, matched against the host:
    `*.example.net` covers `hub.example.net` and `example.net` itself, and
    `hub.*` covers that host on any domain. A CIDR is matched against the host
    as an address, and against the IPv4 address an IPv6 host carries. An entry
    that holds a dot is a domain, matched against the host and every host under
    it, so `example.net` covers `hub.example.net` and refuses nothing else. An
    entry with no dot is matched against the host and then looked for in the
    whole URL, which is the plain-text case.
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
        return _in_network(addresses, network)

    if entry.startswith("."):
        return host == entry[1:] or host.endswith(entry)

    try:
        literal = ipaddress.ip_address(entry)
    except ValueError:
        literal = None
    if literal is not None:
        return literal in addresses

    if host == entry or host.endswith(f".{entry}"):
        return True

    # An entry that holds a dot is a domain, and a domain is matched against the
    # host alone. Without this, `example.net` would also refuse
    # `example.net.other.example`, which is somebody else's host.
    if "." in entry:
        return False

    # A plain word is looked for in the whole URL. This is the simple case: the
    # text a person does not want fetched, wherever it appears.
    return entry in url.lower() or entry in host


def block_reason(
    url: str,
    blocked: list[str] | tuple[str, ...],
    *,
    resolve: bool = True,
) -> str | None:
    """Why `url` may not be fetched, or `None` when it may.

    `blocked` is the operator's list and it is the whole policy for a place. An
    empty list still refuses a scheme that is not `http` or `https` and a URL
    this check cannot read as the fetch would. The reason names the rule and
    never the whole URL, so a log line holds no page a person read.

    This never raises. A URL it cannot read is refused, with the reason.
    """
    raw = (url or "").strip()
    if not raw:
        return "the tool asked for no URL"

    try:
        scheme = urlsplit(raw).scheme.lower()
    except ValueError:
        return "the URL cannot be read"
    if scheme not in ALLOWED_SCHEMES:
        return f"the scheme {scheme or 'none'!r} is not http or https"

    try:
        host, address = _host_of(raw)
    except UnreadableUrl as exc:
        return str(exc)
    except ValueError:
        return "the URL cannot be read"

    addresses = _embedded(address) if address is not None else []
    for entry in blocked:
        if _matches(entry, raw, host, addresses):
            return f"the host is covered by the blocked entry {entry!r}"

    if resolve and address is None:
        networks = [n for n in (_network(e) for e in blocked) if n is not None]
        if networks:
            found = [a for r in _resolved(host) for a in _embedded(r)]
            for network in networks:
                if _in_network(found, network):
                    return f"the host resolves into the blocked entry {str(network)!r}"

    return None
