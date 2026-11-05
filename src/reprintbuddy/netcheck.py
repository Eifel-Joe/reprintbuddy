"""Can this machine listen on an address? Used before a listen address is saved."""

from __future__ import annotations

import socket


def can_bind(host: str) -> bool:
    """True if `host` resolves and every one of its addresses can be bound on this machine.

    A listen address that fails here would stop the server from starting after the
    next restart and lock everybody out.
    """
    if not host or not host.strip():
        return False
    try:
        infos = socket.getaddrinfo(host.strip(), 0, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except (socket.gaierror, UnicodeError):
        return False
    if not infos:
        return False
    # The server binds every address a name resolves to (and fails if one is not ours),
    # so every one of them has to work.
    for family, kind, proto, _name, address in infos:
        try:
            with socket.socket(family, kind, proto) as probe:
                probe.bind(address)
        except OSError:
            return False
    return True
