"""A bound on every request body, before anything reads it (v0.3.9 T0).

Root: Uvicorn runs on h11 (Reprintbuddy installs plain `uvicorn`, no httptools). h11 frames a
request that carries both `Content-Length` and `Transfer-Encoding: chunked` by the chunked
encoding ("T-E beats C-L", h11 0.16 `_connection.py` `_body_framing`), while the
`content-length` header still reaches the app unchanged. A route that trusted the header could
be passed with a small number and a body of any size. And every writing route reads the form
(`verify_csrf`, the login page before anyone signs in): Starlette keeps file parts of any size
in a temporary file, so a form field nobody asked for was enough.

Fix: one pure ASGI middleware for every request.
- `Transfer-Encoding` of any kind → 411. Browsers send forms and `fetch` bodies with a length.
  On an upload path the route answers instead (with a page of its own), and may read nothing.
- A length that is not one plain number → 400; over the limit of the path → 413. Paths with a
  limit of their own (the uploads) are let through with a longer declaration, so their route can
  refuse with a page of its own before reading (`upload_length_ok`).
- What the app reads is counted: past the declared length or the limit the app gets an error and
  the client a 413, should a server ever deliver more than it announced.

Rejected: a check in each route only (every `request.form()` would need it, and the CSRF check
runs before the route); a limit in Uvicorn (it has none for the body); relying on
`python-multipart` (it bounds non-file parts only). Tests: tests/web/test_body_limit.py.
"""

from __future__ import annotations

from collections.abc import Mapping

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Everything but the file uploads is a small form: PEM files are capped at 64 KiB each
# (`routes/settings.py`), the largest forms carry a few hundred fields.
DEFAULT_LIMIT = 1024 * 1024

# Room for the CSRF field and the multipart boundaries around an uploaded file.
MULTIPART_OVERHEAD = 64 * 1024


class _TooLarge(Exception):
    """Raised into the app when it reads past the bound."""


def _header(scope: Scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value
    return None


def route_path(scope: Scope) -> str:
    """The path as the router sees it: without `root_path`, which is removed only at a "/" boundary.

    Behind a proxy under a sub-path the server passes `path` with the prefix and `root_path` with the
    prefix alone (ASGI 3.0); the limits below are written without it. Anything that does not clearly start
    with the prefix stays as it is, so it finds no entry and gets the small default limit. The rule is the
    one of Starlette's router (`_utils.get_route_path`), written out here because that function is private:
    the limit must belong to the route that runs, not to a lookalike (v0.3.10 p133)."""
    path = scope.get("path", "")
    root = scope.get("root_path") or ""
    if not root or not path.startswith(root):
        return path
    if path == root:
        return ""
    return path[len(root):] if path[len(root)] == "/" else path


def declared_length(headers: Mapping[str, str]) -> int | None:
    """The length a request declares, or None when it is missing or not one plain number."""
    value = headers.get("content-length")
    if value is None or not value.isdigit() or len(value) > 15:
        return None
    return int(value)


def upload_length_ok(request, limit: int) -> bool:
    """The first check of an upload route, before anything reads the body.

    No `Transfer-Encoding`, a plain `Content-Length`, at most `limit` (file plus form). The
    middleware has already refused the first two; checking again keeps the route safe on its own.
    """
    if request.headers.get("transfer-encoding") is not None:
        return False
    length = declared_length(request.headers)
    return length is not None and length <= limit


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, default_limit: int = DEFAULT_LIMIT, limits: Mapping[str, int] | None = None):
        self.app = app
        self.default_limit = default_limit
        self.limits = dict(limits or {})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        own = self.limits.get(route_path(scope))
        limit = self.default_limit if own is None else own
        if _header(scope, b"transfer-encoding") is not None:
            if own is None:
                await _answer(send, 411, b"Length Required")
                return
            declared = 0  # its route refuses with a page of its own; it may read nothing
        else:
            raw = _header(scope, b"content-length")
            if raw is None:
                declared = 0  # h11 reads no body without a length
            else:
                text = raw.decode("latin-1")
                if not text.isdigit() or len(text) > 15:
                    await _answer(send, 400, b"Bad Content-Length")
                    return
                declared = int(text)
            if own is None and declared > limit:
                await _answer(send, 413, b"Request Entity Too Large")
                return
        bound = min(declared, limit)
        received = 0
        started = False

        async def counted_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > bound:
                    raise _TooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except _TooLarge:
            if not started:
                await _answer(send, 413, b"Request Entity Too Large")
        except BaseExceptionGroup as group:
            # The `BaseHTTPMiddleware` layers inside (gate, tick, update guard) run the app in a
            # task group and hand the error on wrapped (security review v0.3.9). Only a group
            # made of nothing but `_TooLarge` becomes a 413; anything else goes on as it came.
            rest = group.split(_TooLarge)[1]
            if rest is not None:
                raise
            if not started:
                await _answer(send, 413, b"Request Entity Too Large")


async def _answer(send: Send, status: int, text: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"connection", b"close")],
        }
    )
    await send({"type": "http.response.body", "body": text})
