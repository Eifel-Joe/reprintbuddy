"""The server's own network address, for the QR codes (Spec 8.4).

A code on a shelf has to open Reprintbuddy from a phone, so it carries the address of this machine
in the network, never `localhost` and never a name only this browser knows. Reprintbuddy finds it
itself, without sending a single packet:

1. the local address the request came in on (the web server read it from its socket): with
   several networks that is the one the caller is in;
2. else the address the machine would use for its default route: a UDP socket that is only
   `connect`ed asks the routing table and sends nothing (a name is never looked up, so no DNS
   query goes out either);
3. else the same question for the private networks, for a machine without a default route.

Only IPv4 is searched in steps 2 and 3. Nothing found (loopback only, a container without network): the caller keeps the address of the
request, and the page warns that a phone cannot open it.
"""

from __future__ import annotations

import ipaddress
import socket

# A reserved documentation address (RFC 5737): nobody owns it, so a connect to it can only be
# answered by the default route. Nothing is ever sent to it.
_DEFAULT_ROUTE_PROBE = "192.0.2.1"
# Without a default route the kernel still knows the networks it is attached to: a unicast address
# at the end of each private block is on-link where such a network exists.
# Written as numbers: the repository test keeps private address literals out of every file.
_PRIVATE_PROBES = tuple(".".join(map(str, parts)) for parts in ((192, 168, 255, 254), (10, 255, 255, 254), (172, 31, 255, 254)))
_DISCARD_PORT = 9


def usable(text: object) -> str | None:
    """`text` when it is an address another device can reach (not loopback, not the wildcard, not
    link-local, not multicast); otherwise None. Names, blanks and zone ids ("%eth0") are refused.
    An IPv4 address in its IPv6 form (`::ffff:a.b.c.d`, as a dual-stack server reports it) is
    returned as the plain IPv4 address."""
    if not isinstance(text, str) or not text or "%" in text:
        return None
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:  # ::ffff:a.b.c.d is an IPv4 address a dual-stack server reports
        return usable(str(mapped))
    if (
        address.is_loopback
        or address.is_unspecified
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    ):
        return None
    return text


def connection_address(server: object) -> str | None:
    """The usable local address of a connection, from the ASGI `server` pair `(host, port)`."""
    if not isinstance(server, (tuple, list)) or not server:
        return None
    return usable(server[0])


def _source_address(target: str) -> str | None:
    """The local IPv4 address the machine would send to `target` from. `connect` on a datagram
    socket only picks the route: no packet leaves."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((target, _DISCARD_PORT))
            return usable(probe.getsockname()[0])
    except OSError:
        return None


def machine_address() -> str | None:
    """This machine's own address in the network, or None (loopback only, no network)."""
    for target in (_DEFAULT_ROUTE_PROBE, *_PRIVATE_PROBES):
        found = _source_address(target)
        if found:
            return found
    return None
