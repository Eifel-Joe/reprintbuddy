"""The printer's address: from the settings only, looked up once, never this machine (Spec 8.5)."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Collection
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as LookupTimeout

from reprintbuddy.labels.errors import PrinterAddressRefused, PrinterUnreachable

RAW_PORT = 9100
RESOLVE_TIMEOUT = 3.0  # seconds a name may take to resolve (the system resolver has no limit of its own)
# A lookup that hangs keeps its worker thread; two of them at most, further lookups wait their turn
# and give up after their own limit.
_LOOKUPS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="label-lookup")


_THIS_NETWORK = ipaddress.ip_network("0.0.0.0/8")


def _parsed(address: str):
    """The address as an object; an IPv4 address in IPv6 form is the plain IPv4 address (so
    ::ffff:127.0.0.1 is loopback on every Python version); None when it is not an address."""
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None
    mapped = getattr(parsed, "ipv4_mapped", None)
    return mapped if mapped is not None else parsed


def is_local_or_wildcard(address: str) -> bool:
    """An address that is never a label printer in the network: this machine (loopback), the wildcard,
    "this network" (0.x), link-local (169.254.x.x, fe80::), multicast, broadcast and the reserved blocks."""
    parsed = _parsed(address)
    if parsed is None:
        return False
    return (
        parsed.is_loopback
        or parsed.is_unspecified
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or (parsed.version == 4 and parsed in _THIS_NETWORK)
    )


def is_own(address: str, own: Collection[str]) -> bool:
    """Is `address` one of the server's own addresses (`own`, as text)?"""
    parsed = _parsed(address)
    return parsed is not None and any(parsed == _parsed(item) for item in own)


def resolve(
    host: str,
    port: int = RAW_PORT,
    *,
    allow_local: bool = False,
    resolve_timeout: float = RESOLVE_TIMEOUT,
    refuse: Collection[str] = (),
) -> str:
    """The address to talk to: an IP address as it is, a name looked up once (so the check and the
    sending cannot see two different answers). Refused: what `is_local_or_wildcard` names and the
    addresses in `refuse` (the server's own); a name that gives nothing at all is unreachable."""
    text = host.strip()
    try:
        candidates = [str(ipaddress.ip_address(text))]
    except ValueError:
        try:
            found = _LOOKUPS.submit(socket.getaddrinfo, text, port, type=socket.SOCK_STREAM).result(
                timeout=resolve_timeout
            )
        except (OSError, UnicodeError, LookupTimeout):
            raise PrinterUnreachable() from None
        candidates = [info[4][0] for info in found]
    for candidate in candidates:
        if allow_local or not (is_local_or_wildcard(candidate) or is_own(candidate, refuse)):
            return candidate
    if candidates:
        raise PrinterAddressRefused()
    raise PrinterUnreachable()
