"""Every cookie of a request that came over HTTPS is marked Secure (p177, v0.3.14).

Root: the session cookie was marked Secure only when Reprintbuddy has the certificate itself
(`SessionMiddleware(https_only=...)`, fixed at start). Behind a proxy that does HTTPS it went out without
the mark, and a browser would also send it over plain HTTP to the same host.

The decision follows the scheme of the request. With an own certificate that is https anyway. Behind a
proxy it is https only when uvicorn's proxy layer has taken `X-Forwarded-Proto: https` from a proxy it
trusts, and `serve` lets it trust the same machine only (`cli.TRUSTED_PROXIES`, Spec 11.4: a proxy on the
same system, Reprintbuddy listening on 127.0.0.1). A device in the network cannot claim HTTPS for a plain
connection, and plain HTTP in the LAN keeps cookies without the mark, so signing in there goes on working.

Rejected: a switch "behind an HTTPS proxy" under Settings - one more thing to set, and wrong as soon as
someone opens Reprintbuddy directly. Rejected: trusting forwarded headers of any address - anyone in the
network could set them.
Tests: tests/web/test_cookie_proxy.py.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _with_secure(value: bytes) -> bytes:
    attributes = [part.strip().lower() for part in value.split(b";")[1:]]
    return value if b"secure" in attributes else value + b"; Secure"


class SecureCookiesOnHttps:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("scheme") != "https":
            await self.app(scope, receive, send)
            return

        async def send_secure(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (name, _with_secure(value) if name.lower() == b"set-cookie" else value)
                    for name, value in message.get("headers") or []
                ]
            await send(message)

        await self.app(scope, receive, send_secure)
