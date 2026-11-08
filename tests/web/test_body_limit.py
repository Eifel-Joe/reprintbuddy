"""Every request body is bounded before anything reads it (v0.3.9 T0).

Root: Uvicorn runs on h11 here (no httptools). h11 frames a request with both
`Content-Length` and `Transfer-Encoding: chunked` by the chunked encoding ("T-E beats C-L",
h11 `_connection.py` `_body_framing`), yet the `content-length` header still reaches the app.
A check of `Content-Length` alone could be passed with a small number and a large chunked
body; Starlette's `request.form()` keeps file parts of any size in a temporary file, and every
writing route parses the form (`verify_csrf`), the login page before anyone signs in.

The middleware refuses any `Transfer-Encoding`, refuses a declared length over the limit of the
path, and counts what is read so nothing past the limit reaches a route. The TestClient does not
go through h11; the first tests use a real Uvicorn.
"""

from __future__ import annotations

import asyncio
import select
import socket
import threading
import time

import pytest
import uvicorn

from reprintbuddy.web import bodylimit
from reprintbuddy.web.bodylimit import BodyLimitMiddleware, upload_length_ok


# ---- through a real Uvicorn (h11) ------------------------------------------------------------


@pytest.fixture
def server(admin_client):
    config = uvicorn.Config(admin_client.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert srv.started
    yield srv.servers[0].sockets[0].getsockname()[1]
    srv.should_exit = True
    thread.join(timeout=10)


def _status(port: int, head: bytes, body_chunks=()) -> int:
    """Send `head` and then `body_chunks`; the status of the answer (the server may answer early)."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(head)
        # An early answer is read before the body goes out: a server that closes with unread
        # data makes Windows reset the connection and drop what was already received.
        ready, _, _ = select.select([sock], [], [], 2)
        if not ready:
            for chunk in body_chunks:
                sock.sendall(chunk)
        line = b""
        while not line.endswith(b"\r\n"):
            data = sock.recv(1)
            if not data:
                break
            line += data
    return int(line.split()[1])


def _chunked(total: int, size: int = 64 * 1024):
    sent = 0
    while sent < total:
        part = b"x" * min(size, total - sent)
        sent += len(part)
        yield f"{len(part):x}\r\n".encode() + part + b"\r\n"
    yield b"0\r\n\r\n"


def test_chunked_body_with_small_content_length_is_refused(server):
    head = (
        b"POST /login HTTP/1.1\r\nHost: x\r\n"
        b"Content-Type: multipart/form-data; boundary=b\r\n"
        b"Content-Length: 10\r\nTransfer-Encoding: chunked\r\n\r\n"
    )
    assert _status(server, head, _chunked(3 * 1024 * 1024)) == 411


def test_chunked_body_without_content_length_is_refused(server):
    head = b"POST /login HTTP/1.1\r\nHost: x\r\nContent-Type: multipart/form-data; boundary=b\r\nTransfer-Encoding: chunked\r\n\r\n"
    assert _status(server, head, _chunked(3 * 1024 * 1024)) == 411


def test_a_declared_length_over_the_default_limit_is_refused(server):
    length = bodylimit.DEFAULT_LIMIT + 1
    head = (
        b"POST /login HTTP/1.1\r\nHost: x\r\nContent-Type: multipart/form-data; boundary=b\r\n"
        + f"Content-Length: {length}\r\n\r\n".encode()
    )
    assert _status(server, head) == 413


def test_a_small_form_still_reaches_the_route(server):
    body = b"username=a&password=b&csrf_token=c"
    head = (
        b"POST /login HTTP/1.1\r\nHost: x\r\nContent-Type: application/x-www-form-urlencoded\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
    )
    assert _status(server, head, [body]) == 403  # the route ran: the CSRF check refused it


# ---- the middleware on its own ---------------------------------------------------------------


def _scope(path="/x", headers=()):
    return {"type": "http", "method": "POST", "path": path, "headers": [(k.encode(), v.encode()) for k, v in headers]}


def _receiver(chunks):
    messages = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    return receive


async def _run(middleware, scope, chunks):
    sent = []

    async def send(message):
        sent.append(message)

    await middleware(scope, _receiver(chunks), send)
    starts = [m for m in sent if m["type"] == "http.response.start"]
    return starts[0]["status"] if starts else None


def _reader_app(seen: list):
    async def app(scope, receive, send):
        while True:
            message = await receive()
            seen.append(len(message.get("body", b"")))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return app


async def test_transfer_encoding_never_reaches_the_app():
    seen: list = []
    mw = BodyLimitMiddleware(_reader_app(seen))
    assert await _run(mw, _scope(headers=[("transfer-encoding", "chunked")]), [b"x"]) == 411
    assert seen == []


async def test_over_the_limit_never_reaches_the_app():
    seen: list = []
    mw = BodyLimitMiddleware(_reader_app(seen), default_limit=10)
    assert await _run(mw, _scope(headers=[("content-length", "11")]), [b"x" * 11]) == 413
    assert seen == []


@pytest.mark.parametrize("value", ["abc", "-1", "1,1", " "])
async def test_an_unreadable_length_is_refused(value):
    seen: list = []
    mw = BodyLimitMiddleware(_reader_app(seen))
    assert await _run(mw, _scope(headers=[("content-length", value)]), [b""]) == 400
    assert seen == []


async def test_more_than_declared_is_cut_off_while_reading():
    """Defence in depth: should a server ever pass more than the header said."""
    seen: list = []
    mw = BodyLimitMiddleware(_reader_app(seen), default_limit=100)
    assert await _run(mw, _scope(headers=[("content-length", "10")]), [b"x" * 8, b"x" * 8]) == 413
    assert sum(seen) <= 10


async def test_without_a_length_no_body_is_passed_on():
    seen: list = []
    mw = BodyLimitMiddleware(_reader_app(seen), default_limit=100)
    assert await _run(mw, _scope(), [b"x" * 8]) == 413


async def test_a_path_of_its_own_lets_its_route_answer_a_too_long_declaration():
    """The upload routes refuse with a page of their own; the middleware still counts."""
    called = []

    async def app(scope, receive, send):
        called.append(True)  # does not read the body
        await send({"type": "http.response.start", "status": 303, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = BodyLimitMiddleware(app, default_limit=10, limits={"/up": 100})
    assert await _run(mw, _scope("/up", [("content-length", "1000")]), [b"x"]) == 303
    assert called == [True]


async def test_a_path_of_its_own_is_cut_off_at_its_limit_when_read():
    seen: list = []
    mw = BodyLimitMiddleware(_reader_app(seen), default_limit=10, limits={"/up": 100})
    assert await _run(mw, _scope("/up", [("content-length", "1000")]), [b"x" * 60, b"x" * 60]) == 413
    assert sum(seen) <= 100


async def test_within_the_limit_the_body_passes_unchanged():
    seen: list = []
    mw = BodyLimitMiddleware(_reader_app(seen), default_limit=100)
    assert await _run(mw, _scope(headers=[("content-length", "16")]), [b"x" * 8, b"x" * 8]) == 200
    assert seen == [8, 8]


async def test_other_scopes_pass_through():
    called = []

    async def app(scope, receive, send):
        called.append(scope["type"])

    await BodyLimitMiddleware(app)({"type": "lifespan"}, None, None)
    assert called == ["lifespan"]


# ---- the check the upload routes make first ---------------------------------------------------


class _Req:
    def __init__(self, headers):
        self.headers = {k.lower(): v for k, v in headers.items()}


@pytest.mark.parametrize(
    ("headers", "ok"),
    [
        ({"content-length": "100"}, True),
        ({"content-length": "101"}, False),
        ({}, False),
        ({"content-length": "x"}, False),
        ({"content-length": "10", "transfer-encoding": "chunked"}, False),
    ],
)
def test_upload_length_ok(headers, ok):
    assert upload_length_ok(_Req(headers), 100) is ok


def test_the_upload_routes_have_their_own_limits(client):
    limits = client.app.state.body_limits
    from reprintbuddy.ops import fullbackup as fb

    assert limits["/settings/restore/upload"] >= fb.MAX_UPLOAD_BYTES
    assert all(limit > bodylimit.DEFAULT_LIMIT for limit in limits.values())


async def test_transfer_encoding_on_an_upload_path_is_left_to_its_route_which_reads_nothing():
    """The route refuses with a page of its own (`upload_length_ok`); a read is cut off at once."""
    seen: list = []
    mw = BodyLimitMiddleware(_reader_app(seen), default_limit=10, limits={"/up": 100})
    headers = [("content-length", "5"), ("transfer-encoding", "chunked")]
    assert await _run(mw, _scope("/up", headers), [b"x" * 50]) == 413
    assert seen == []

    async def refusing(scope, receive, send):
        await send({"type": "http.response.start", "status": 303, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = BodyLimitMiddleware(refusing, default_limit=10, limits={"/up": 100})
    assert await _run(mw, _scope("/up", headers), [b"x" * 50]) == 303
