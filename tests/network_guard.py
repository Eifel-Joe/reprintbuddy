"""Keeps the tests off the network (Spec 12: a Bambuddy is never contacted).

Every test address ends in `.test`, a reserved name that never resolves for real. A lookup of
such a name means a request that no respx route caught; calls caught by respx never resolve
anything. The guard records the name and fails the test at its end, so a caller that
swallows the resulting error (a page that shows "not reachable") cannot hide the leak.
Other names go to the real resolver: loopback servers and the network tests need it.
"""

from __future__ import annotations

import socket


class NetworkGuard:
    def __init__(self, real=socket.getaddrinfo) -> None:
        self.real = real
        self.leaks: list[str] = []

    def getaddrinfo(self, host, *args, **kwargs):
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        if isinstance(name, str) and name.rstrip(".").lower().endswith(".test"):
            self.leaks.append(name)
            raise socket.gaierror(socket.EAI_NONAME, f"test host {name} must not be resolved")
        return self.real(host, *args, **kwargs)

    def report(self) -> str | None:
        if not self.leaks:
            return None
        hosts = ", ".join(sorted(set(self.leaks)))
        return f"the test looked up a test host without a respx route: {hosts}"
