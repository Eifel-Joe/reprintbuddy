"""The printer's address: from the settings only, looked up once, never this machine (Spec 8.5)."""

from __future__ import annotations

import ipaddress
import socket

from reprintbuddy.labels.errors import PrinterAddressRefused, PrinterUnreachable

RAW_PORT = 9100


def is_local_or_wildcard(address: str) -> bool:
    """Loopback, the wildcard or a multicast address: never the address of a printer."""
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast


def resolve(host: str, port: int = RAW_PORT, *, allow_local: bool = False) -> str:
    """The address to talk to: an IP address as it is, a name looked up once (so the check and the
    sending cannot see two different answers). Refused: loopback, the wildcard, multicast; a name
    that gives nothing at all is unreachable."""
    text = host.strip()
    try:
        candidates = [str(ipaddress.ip_address(text))]
    except ValueError:
        try:
            found = socket.getaddrinfo(text, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            raise PrinterUnreachable() from None
        candidates = [info[4][0] for info in found]
    for candidate in candidates:
        if allow_local or not is_local_or_wildcard(candidate):
            return candidate
    if candidates:
        raise PrinterAddressRefused()
    raise PrinterUnreachable()
