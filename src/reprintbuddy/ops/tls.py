"""Check that a certificate and a private key can be used together.

A pair that does not load would stop Reprintbuddy from starting after the next
restart and lock everybody out, so it is checked before it is stored.
"""

from __future__ import annotations

import os
import ssl
import tempfile
from pathlib import Path


def validate_tls_pair(cert_pem: str, key_pem: str) -> None:
    """Raise `ValueError("tls_encrypted")` or `ValueError("tls_invalid")`; return if usable.

    The contents never appear in a message or a log line.
    """
    if "ENCRYPTED" in key_pem:
        raise ValueError("tls_encrypted")
    with tempfile.TemporaryDirectory(prefix="reprintbuddy-tls-") as folder:
        paths = []
        for name, content in (("cert.pem", cert_pem), ("key.pem", key_pem)):
            path = Path(folder) / name
            path.write_text(content, encoding="utf-8")
            if os.name == "posix":
                path.chmod(0o600)
            paths.append(path)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            # The password callback keeps OpenSSL from asking on the terminal.
            context.load_cert_chain(str(paths[0]), str(paths[1]), password=lambda: b"")
        except (ssl.SSLError, OSError, ValueError):
            raise ValueError("tls_invalid") from None
