"""A deployment under a proxy sub-path is not supported: say so once in the log (Spec 11.4; v0.3.11 p139).

Behind a reverse proxy Reprintbuddy runs under its own address or its own port. Under a sub-path
(`https://host/reprintbuddy/`) the pages build their links and redirects from "/", and they would point
past the proxy; making that work is a feature of its own that the spec does not ask for. What can be
done cheaply is to recognise the setup and say what is wrong, once, instead of leaving a half-working
installation that nobody can trace: the server hands over a `root_path` (Uvicorn's `--root-path`), or a
proxy sends `X-Forwarded-Prefix`.

The text is fixed. Nothing of the request goes into the log: the header comes from the client and a line
break in it would forge log lines. One warning per application instance, not one per request.
Tests: tests/web/test_subpath_warning.py.
"""

from __future__ import annotations

import logging

from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger("reprintbuddy.web")

WARNING = (
    "Reprintbuddy is reached through a sub-path (a root path or an X-Forwarded-Prefix header). "
    "That is not supported: behind a reverse proxy it works only under its own address or port, "
    "so links and redirects will point to the wrong place. See the README (reverse proxy)."
)


def _under_sub_path(scope: Scope) -> bool:
    if scope.get("root_path"):
        return True
    return any(key.lower() == b"x-forwarded-prefix" for key, _ in scope.get("headers") or ())


class SubPathWarning:
    def __init__(self, app: ASGIApp):
        self.app = app
        self._said = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not self._said and _under_sub_path(scope):
            self._said = True
            log.warning(WARNING)
        await self.app(scope, receive, send)
