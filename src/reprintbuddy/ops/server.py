"""Which address is the server running on, and which one is saved?

Host, port and HTTPS only take effect after a restart, so the settings page compares
what the running process was started with against what is stored now.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy.orm import Session

from reprintbuddy import settings as st

_WILDCARDS = frozenset({"0.0.0.0", "::", ""})


def connect_host(bind_host: str) -> str:
    """The address to use for a request to ourselves: a wildcard means "this machine"."""
    return "127.0.0.1" if bind_host in _WILDCARDS else bind_host


def base_url(host: str, port: int, tls: bool) -> str:
    shown = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{'https' if tls else 'http'}://{shown}:{port}"


@dataclass(frozen=True)
class EffectiveServer:
    host: str
    port: int
    tls_digest: str | None

    @property
    def tls(self) -> bool:
        return self.tls_digest is not None


def tls_digest(cert: str | None, key: str | None) -> str | None:
    """A fingerprint of certificate and key, to notice a change; never shown anywhere."""
    if not cert or not key:
        return None
    digest = hashlib.sha256()
    for part in (cert, key):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def effective_from_settings(session: Session) -> EffectiveServer:
    return EffectiveServer(
        host=str(st.get_setting(session, "host")),
        port=int(st.get_setting(session, "port")),  # type: ignore[arg-type]
        tls_digest=tls_digest(
            st.get_setting(session, "tls_cert"),  # type: ignore[arg-type]
            st.get_setting(session, "tls_key"),  # type: ignore[arg-type]
        ),
    )


def address_changed(saved: EffectiveServer, running: EffectiveServer) -> bool:
    """Is the address (host, port, HTTP or HTTPS) different? Another certificate alone is not."""
    return (saved.host, saved.port, saved.tls) != (running.host, running.port, running.tls)
