"""Bambuddy's live channel, client side (B2 Task 9, Spec 7.6).

The token comes over HTTP (respx); the connection is a scripted stand-in passed as
`connect`, and `sleep` and `now` are injected, so no test waits for real time. Three tests
run the real `websockets` library against a local server on 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

import httpx
import pytest
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from reprintbuddy.bambuddy import live
from reprintbuddy.bambuddy.client import (
    BambuddyRejected,
    BambuddyUnavailable,
    BambuddyUnexpected,
    HttpBambuddyClient,
)
from fixtures import API, KEY, client

# One TLS context for every client of this module: building it per call costs most of a
# second, and the listener asks for a token at almost every attempt.
_TLS = httpx.create_ssl_context()


def quick_client() -> HttpBambuddyClient:
    return client(lambda: httpx.AsyncClient(verify=_TLS))


TOKEN_URL = f"{API}/auth/ws-token"
WS = "ws://bambuddy.test:8000/api/v1/ws"


# ---- the token and the address (HttpBambuddyClient) --------------------------------------


async def test_ws_token(respx_mock):
    route = respx_mock.post(TOKEN_URL).respond(json={"token": "t1"})
    assert await client().ws_token() == "t1"
    request = route.calls.last.request
    assert request.headers["X-API-Key"] == KEY
    assert request.content == b""


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (httpx.Response(403, json={"detail": "Missing permission"}), BambuddyRejected),
        (httpx.Response(200, json={}), BambuddyUnexpected),
        (httpx.Response(200, json={"token": ""}), BambuddyUnexpected),
        (httpx.Response(200, json={"token": 7}), BambuddyUnexpected),
        (httpx.Response(200, json=["t1"]), BambuddyUnexpected),
        (httpx.Response(500), BambuddyUnavailable),
    ],
    ids=["403", "no-token", "empty-token", "number", "list", "500"],
)
async def test_ws_token_errors(respx_mock, answer, error):
    respx_mock.post(TOKEN_URL).mock(return_value=answer)
    with pytest.raises(error) as caught:
        await client().ws_token()
    if error is BambuddyRejected:
        assert caught.value.status == 403
    if error is BambuddyUnavailable:
        assert not isinstance(caught.value, BambuddyUnexpected)


def test_ws_url():
    plain = HttpBambuddyClient("http://bambuddy.test:8000/", KEY, httpx.AsyncClient)
    assert plain.ws_url("a+b") == "ws://bambuddy.test:8000/api/v1/ws?token=a%2Bb"
    secure = HttpBambuddyClient("https://bambuddy.test:8000/", KEY, httpx.AsyncClient)
    assert secure.ws_url("t1") == "wss://bambuddy.test:8000/api/v1/ws?token=t1"
    prefixed = HttpBambuddyClient("http://bambuddy.test/bb", KEY, httpx.AsyncClient)
    assert prefixed.ws_url("x/y&z") == "ws://bambuddy.test/bb/api/v1/ws?token=x%2Fy%26z"


# ---- a scripted connection ---------------------------------------------------------------


def closed(code: int) -> ConnectionClosedError:
    return ConnectionClosedError(Close(code, ""), None)


def closed_ok() -> ConnectionClosedOK:
    return ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)


def refused(status: int) -> InvalidStatus:
    return InvalidStatus(Response(status, "Forbidden", Headers()))


class FakeConnection:
    """`recv` plays a script: texts are messages, exceptions are raised, callables are run.

    An exhausted script waits forever (an open, quiet connection).
    """

    def __init__(self, script) -> None:
        self.script = list(script)
        self.closed = False
        self.close_codes: list[int] = []
        self.waiting = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        self.closed = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_codes.append(code)

    async def recv(self):
        while self.script:
            item = self.script.pop(0)
            if callable(item):
                item()
                continue
            if isinstance(item, BaseException):
                raise item
            return item
        self.waiting.set()
        await asyncio.Event().wait()


class FakeConnect:
    """Stands in for `websockets.asyncio.client.connect`.

    Each session is a script for one connection, or an exception (or a function of the URL
    giving one) raised while connecting.
    """

    def __init__(self, *sessions) -> None:
        self.sessions = list(sessions)
        self.urls: list[str] = []
        self.options: list[dict] = []
        self.connections: list[FakeConnection] = []

    def __call__(self, url: str, **options):
        self.urls.append(url)
        self.options.append(options)
        session = self.sessions.pop(0) if self.sessions else []
        if callable(session):
            session = session(url)
        if isinstance(session, BaseException):
            return _Failing(session)
        connection = FakeConnection(session)
        self.connections.append(connection)
        return connection


class _Failing:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def __aenter__(self):
        raise self.exc

    async def __aexit__(self, *exc) -> None:  # pragma: no cover - never entered
        return None


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float):
        def step() -> None:
            self.t += seconds

        return step


class Sleeps:
    """Records each pause; the pause number `stop_at` cancels, ending `run()`."""

    def __init__(self, stop_at: int) -> None:
        self.delays: list[float] = []
        self.stop_at = stop_at
        self.watch = lambda: None  # runs at the start of each pause

    async def __call__(self, delay: float) -> None:
        self.watch()
        self.delays.append(delay)
        if len(self.delays) >= self.stop_at:
            raise asyncio.CancelledError


class Events:
    def __init__(self) -> None:
        self.kinds: list[str] = []

    def __call__(self, kind: str) -> None:
        self.kinds.append(kind)


def tokens(respx_mock, *names: str):
    """`ws-token` answers with these tokens in turn."""
    return respx_mock.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(200, json={"token": name}) for name in names]
    )


def listener(connect, on_event, sleeps, clock=None) -> live.LiveListener:
    return live.LiveListener(quick_client(), on_event, connect=connect, sleep=sleeps, now=clock or Clock())


async def stop(task: asyncio.Task) -> None:
    """Cancel `task` and wait for it; run in `finally`, so no failed assert leaves it running."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def run_until_stopped(subject: live.LiveListener) -> None:
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(subject.run(), timeout=5)


# ---- the listener ------------------------------------------------------------------------


def test_constants():
    assert live.TRIGGER_TYPES == frozenset(
        {"print_start", "print_complete", "queue_item_acked", "queue_item_failed"}
    )
    assert live.TOKEN_MAX_AGE == 55 * 60
    assert live.RETRY_DELAYS == (5, 15, 60, 300)
    assert live.NO_RIGHT_DELAY == 900


def test_logged_address_has_no_query_and_no_user():
    assert live._without_query("wss://u:pw@bambuddy.test:8000/api/v1/ws?token=t1") == (
        "wss://bambuddy.test:8000/api/v1/ws"
    )


async def test_triggers_only_on_trigger_types(respx_mock):
    tokens(respx_mock, "t1")
    messages = [
        json.dumps({"type": "printer_status", "printer_id": 1}),
        json.dumps({"type": "print_complete", "printer_id": 1}),
        "kein json",
        "[]",
        json.dumps({"type": 5}),
        json.dumps({"type": "queue_item_failed", "item_id": 3}),
        json.dumps({"type": "inventory_changed"}),
        closed(1006),
    ]
    events = Events()
    connect = FakeConnect(messages)
    await run_until_stopped(listener(connect, events, Sleeps(1)))
    assert events.kinds == ["connected", "print_complete", "queue_item_failed"]
    assert connect.urls == [f"{WS}?token=t1"]


async def test_bytes_messages_are_read(respx_mock):
    tokens(respx_mock, "t1")
    events = Events()
    connect = FakeConnect([json.dumps({"type": "print_start"}).encode(), closed(1006)])
    await run_until_stopped(listener(connect, events, Sleeps(1)))
    assert events.kinds == ["connected", "print_start"]


async def test_async_callback(respx_mock):
    tokens(respx_mock, "t1")
    seen: list[str] = []

    async def on_event(kind: str) -> None:
        await asyncio.sleep(0)
        seen.append(kind)

    connect = FakeConnect([json.dumps({"type": "queue_item_acked"}), closed(1006)])
    await run_until_stopped(listener(connect, on_event, Sleeps(1)))
    assert seen == ["connected", "queue_item_acked"]


async def test_callback_error_does_not_stop(respx_mock, caplog):
    tokens(respx_mock, "t1")
    seen: list[str] = []

    def on_event(kind: str) -> None:
        seen.append(kind)
        if len(seen) == 1:
            raise RuntimeError("callback broke")

    connect = FakeConnect([json.dumps({"type": "print_start"}), closed(1006)])
    with caplog.at_level(logging.ERROR, logger="reprintbuddy.bambuddy.live"):
        await run_until_stopped(listener(connect, on_event, Sleeps(1)))
    assert seen == ["connected", "print_start"]
    assert "callback broke" in caplog.text
    assert any(record.exc_info for record in caplog.records)


async def test_reconnect_with_pause_and_token_reuse(respx_mock):
    route = tokens(respx_mock, "t1")
    events = Events()
    connect = FakeConnect([closed(1006)], [closed_ok()], [])
    sleeps = Sleeps(3)
    task = asyncio.create_task(listener(connect, events, sleeps).run())
    try:
        for _ in range(1000):
            await asyncio.sleep(0)
            if len(connect.connections) == 3 and connect.connections[2].waiting.is_set():
                break
        assert sleeps.delays == [5, 15]
        assert route.call_count == 1
        assert connect.urls == [f"{WS}?token=t1"] * 3
        assert events.kinds == ["connected"] * 3
    finally:
        await stop(task)


async def test_retry_delays_grow_to_the_last(respx_mock):
    tokens(respx_mock, "t1")
    connect = FakeConnect(*[OSError("down")] * 6)
    sleeps = Sleeps(6)
    await run_until_stopped(listener(connect, Events(), sleeps))
    assert sleeps.delays == [5, 15, 60, 300, 300, 300]


async def test_token_unavailable_waits(respx_mock):
    respx_mock.post(TOKEN_URL).respond(500)
    subject = listener(FakeConnect(), Events(), Sleeps(2))
    await run_until_stopped(subject)
    assert subject.state == "waiting"


async def test_token_renewed_when_old(respx_mock):
    route = tokens(respx_mock, "t1", "t2")
    clock = Clock()
    connect = FakeConnect([clock.advance(56 * 60), closed(1006)], [closed(1006)])
    await run_until_stopped(listener(connect, Events(), Sleeps(2), clock))
    assert route.call_count == 2
    assert connect.urls == [f"{WS}?token=t1", f"{WS}?token=t2"]


async def test_token_kept_when_young(respx_mock):
    route = tokens(respx_mock, "t1", "t2")
    clock = Clock()
    connect = FakeConnect([clock.advance(54 * 60), closed(1006)], [closed(1006)])
    await run_until_stopped(listener(connect, Events(), Sleeps(2), clock))
    assert route.call_count == 1
    assert connect.urls == [f"{WS}?token=t1"] * 2


@pytest.mark.parametrize("refusal", [closed(4401), refused(403)], ids=["close-4401", "handshake-403"])
async def test_4401_refetches_token_once(respx_mock, refusal):
    route = tokens(respx_mock, "t1", "t2")
    sleeps = Sleeps(1)
    at_connect: list[tuple[int, list[float]]] = []

    def on_event(kind: str) -> None:
        at_connect.append((route.call_count, list(sleeps.delays)))

    connect = FakeConnect(refusal, [closed(1006)])
    await run_until_stopped(listener(connect, on_event, sleeps))
    assert at_connect == [(2, [])]
    assert connect.urls == [f"{WS}?token=t1", f"{WS}?token=t2"]
    assert sleeps.delays == [5]


@pytest.mark.parametrize("cause", ["token-403", "4401-twice", "handshake-403-twice"])
async def test_no_right(respx_mock, cause):
    if cause == "token-403":
        route = respx_mock.post(TOKEN_URL).respond(403, json={"detail": "Missing permission"})
        connect = FakeConnect()
    else:
        route = tokens(respx_mock, "t1", "t2", "t3", "t4")
        refusal = closed(4401) if cause == "4401-twice" else refused(403)
        connect = FakeConnect(*[refusal] * 4)
    events = Events()
    sleeps = Sleeps(2)
    states: list[str] = []
    subject = listener(connect, events, sleeps)
    sleeps.watch = lambda: states.append(subject.state)
    await run_until_stopped(subject)
    assert states == ["no_right", "no_right"]
    assert sleeps.delays == [live.NO_RIGHT_DELAY, live.NO_RIGHT_DELAY]
    assert events.kinds == []
    if cause == "token-403":
        assert route.call_count == 2  # tried again after the pause
        assert connect.urls == []
    else:
        assert route.call_count == 4
        assert len(connect.urls) == 4


async def test_other_close_codes_wait(respx_mock):
    tokens(respx_mock, "t1")
    connect = FakeConnect([closed(1011)], [closed(4000)])
    subject = listener(connect, Events(), Sleeps(2))
    await run_until_stopped(subject)
    assert subject.state == "waiting"


async def test_backoff_resets_after_stable_connection(respx_mock):
    tokens(respx_mock, "t1")
    clock = Clock()
    connect = FakeConnect(OSError("down"), OSError("down"), [clock.advance(61), closed(1006)])
    sleeps = Sleeps(3)
    await run_until_stopped(listener(connect, Events(), sleeps, clock))
    assert sleeps.delays == [5, 15, 5]


async def test_backoff_not_reset_by_short_connection(respx_mock):
    tokens(respx_mock, "t1")
    clock = Clock()
    connect = FakeConnect(OSError("down"), OSError("down"), [clock.advance(59), closed(1006)])
    sleeps = Sleeps(3)
    await run_until_stopped(listener(connect, Events(), sleeps, clock))
    assert sleeps.delays == [5, 15, 60]


async def test_state_connected_while_open(respx_mock):
    tokens(respx_mock, "t1")
    connect = FakeConnect([])
    subject = listener(connect, Events(), Sleeps(1))
    assert subject.state == "connecting"
    task = asyncio.create_task(subject.run())
    try:
        for _ in range(1000):
            await asyncio.sleep(0)
            if connect.connections and connect.connections[0].waiting.is_set():
                break
        assert subject.state == "connected"
    finally:
        await stop(task)


@pytest.mark.parametrize("where", ["waiting", "receiving"])
async def test_cancel_stops_cleanly(respx_mock, where):
    tokens(respx_mock, "t1")
    sleeping = asyncio.Event()

    async def sleep(delay: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    connect = FakeConnect([closed(1006)] if where == "waiting" else [])
    subject = live.LiveListener(quick_client(), Events(), connect=connect, sleep=sleep, now=Clock())
    task = asyncio.create_task(subject.run())
    try:
        if where == "waiting":
            await asyncio.wait_for(sleeping.wait(), timeout=2)
        else:
            for _ in range(1000):
                await asyncio.sleep(0)
                if connect.connections and connect.connections[0].waiting.is_set():
                    break
            assert subject.state == "connected"
        assert not task.done()
    finally:
        await stop(task)
    assert task.cancelled()  # CancelledError left run(), nothing else
    assert connect.connections[0].closed


async def test_unexpected_error_does_not_leave_run(respx_mock):
    tokens(respx_mock, "t1")

    def broken(url: str, **options):
        raise ValueError("bug in the address")

    subject = live.LiveListener(quick_client(), Events(), connect=broken, sleep=Sleeps(2), now=Clock())
    await run_until_stopped(subject)
    assert subject.state == "waiting"


def pause_lines(caplog) -> list[tuple[int, str]]:
    """(level, text) of the listener's line at each pause."""
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == live.__name__ and "next attempt in" in record.getMessage()
    ]


@pytest.mark.parametrize("cause", ["token-403", "handshake-403", "token-404", "token-500", "network"])
async def test_warning_only_when_the_state_changes(respx_mock, caplog, cause):
    if cause.startswith("token-"):
        respx_mock.post(TOKEN_URL).respond(int(cause[-3:]), json={"detail": "x"})
        connect = FakeConnect()
    else:
        respx_mock.post(TOKEN_URL).respond(json={"token": "t1"})
        refusal = refused(403) if cause == "handshake-403" else OSError("down")
        connect = FakeConnect(*[refusal] * 6)
    with caplog.at_level(logging.DEBUG, logger=live.__name__):
        await run_until_stopped(listener(connect, Events(), Sleeps(3)))
    levels = [level for level, _ in pause_lines(caplog)]
    assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG]
    warnings = [r for r in caplog.records if r.name == live.__name__ and r.levelno >= logging.WARNING]
    assert len(warnings) == 1


async def test_warning_again_after_a_connection(respx_mock, caplog):
    respx_mock.post(TOKEN_URL).respond(json={"token": "t1"})
    connect = FakeConnect(OSError("down"), OSError("down"), [closed(1006)], OSError("down"))
    with caplog.at_level(logging.DEBUG, logger=live.__name__):
        await run_until_stopped(listener(connect, Events(), Sleeps(4)))
    levels = [level for level, _ in pause_lines(caplog)]
    assert levels == [logging.WARNING, logging.DEBUG, logging.WARNING, logging.DEBUG]


async def test_warning_when_waiting_turns_into_no_right(respx_mock, caplog):
    respx_mock.post(TOKEN_URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(403, json={"detail": "x"})]
    )
    with caplog.at_level(logging.DEBUG, logger=live.__name__):
        await run_until_stopped(listener(FakeConnect(), Events(), Sleeps(2)))
    levels = [level for level, _ in pause_lines(caplog)]
    assert levels == [logging.WARNING, logging.WARNING]


async def test_no_right_names_the_right_as_probable_cause(respx_mock, caplog):
    respx_mock.post(TOKEN_URL).respond(403, json={"detail": "x"})
    with caplog.at_level(logging.DEBUG, logger=live.__name__):
        await run_until_stopped(listener(FakeConnect(), Events(), Sleeps(1)))
    [(level, text)] = pause_lines(caplog)
    assert level == logging.WARNING
    assert "probably" in text and "websocket:connect" in text


@pytest.mark.parametrize("case", ["token-403", "4401", "network"])
async def test_secret_never_logged(respx_mock, caplog, case):
    secret = "tok-SECRET-123"
    if case == "token-403":
        respx_mock.post(TOKEN_URL).respond(403, json={"detail": "Missing permission"})
        connect = FakeConnect()
    else:
        tokens(respx_mock, secret, secret, secret)
        if case == "4401":
            connect = FakeConnect(closed(4401), refused(403), [closed(1006)])
        else:
            connect = FakeConnect(lambda url: OSError(f"cannot reach {url}"))
    with caplog.at_level(logging.DEBUG):
        await run_until_stopped(listener(connect, Events(), Sleeps(1)))
    assert caplog.text  # something was logged at all
    assert KEY not in caplog.text
    assert secret not in caplog.text
    assert "token=" not in caplog.text


# ---- the real library against a local server ---------------------------------------------


def quiet_server_log() -> logging.Logger:
    """A logger for the test server: its own lines show the request with the token."""
    server_log = logging.getLogger("tests.live.server")
    server_log.propagate = False
    server_log.addHandler(logging.NullHandler())
    return server_log


async def test_connect_gets_the_quiet_library_logger(respx_mock):
    tokens(respx_mock, "t1")
    connect = FakeConnect([closed(1006)])
    await run_until_stopped(listener(connect, Events(), Sleeps(1)))
    assert connect.options == [{"logger": live.LIBRARY_LOG, "close_timeout": live.CLOSE_WAIT}]
    assert not live.LIBRARY_LOG.isEnabledFor(logging.INFO)


async def test_real_library_logs_no_secret(respx_mock, caplog):
    """The library logs the handshake (`> GET /api/v1/ws?token=…`) at DEBUG; never here."""
    secret = "tok-SECRET-123"
    attempts: list[str] = []

    async def process_request(connection, request):
        attempts.append(request.path)
        if len(attempts) == 1 or request.path != f"/api/v1/ws?token={secret}":
            return connection.respond(403, "")  # the first one is refused, like a stale token
        return None

    async def handler(connection):
        await connection.send(json.dumps({"type": "print_complete"}))
        await connection.wait_closed()

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="websockets")
    # Even turned up by hand, the listener's library logger lets nothing below WARNING out.
    caplog.set_level(logging.DEBUG, logger=live.LIBRARY_LOG.name)
    async with serve(handler, "127.0.0.1", 0, process_request=process_request, logger=quiet_server_log()) as server:
        port = server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        respx_mock.post(f"{base}/api/v1/auth/ws-token").respond(json={"token": secret})
        done = asyncio.Event()
        seen: list[str] = []

        def on_event(kind: str) -> None:
            seen.append(kind)
            if kind == "print_complete":
                done.set()

        subject = live.LiveListener(
            HttpBambuddyClient(base, KEY, lambda: httpx.AsyncClient(verify=_TLS)), on_event
        )
        task = asyncio.create_task(subject.run())
        try:
            await asyncio.wait_for(done.wait(), timeout=5)
        finally:
            await stop(task)
    assert seen == ["connected", "print_complete"]
    assert len(attempts) == 2  # refused once, then accepted
    assert caplog.text  # the listener did log
    assert secret not in caplog.text
    assert KEY not in caplog.text
    assert "token=" not in caplog.text


async def test_real_library_round_trip(respx_mock):
    async def process_request(connection, request):
        if request.path != "/api/v1/ws?token=t1":
            return connection.respond(403, "")
        return None

    async def handler(connection):
        await connection.send(json.dumps({"type": "printer_status"}))
        await connection.send(json.dumps({"type": "print_complete"}))
        await connection.wait_closed()

    async with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        respx_mock.post(f"{base}/api/v1/auth/ws-token").respond(json={"token": "t1"})
        both = asyncio.Event()
        seen: list[str] = []

        def on_event(kind: str) -> None:
            seen.append(kind)
            if len(seen) == 2:
                both.set()

        subject = live.LiveListener(HttpBambuddyClient(base, KEY, lambda: httpx.AsyncClient(verify=_TLS)), on_event)
        task = asyncio.create_task(subject.run())
        try:
            await asyncio.wait_for(both.wait(), timeout=5)
            assert subject.state == "connected"
        finally:
            await stop(task)
        assert task.cancelled()
    assert seen == ["connected", "print_complete"]


async def test_real_library_handshake_refusal_is_no_right(respx_mock):
    """Bambuddy closes before accepting; its server answers the handshake with HTTP 403."""

    async def process_request(connection, request):
        return connection.respond(403, "")

    async def handler(connection):  # pragma: no cover - never accepted
        await connection.wait_closed()

    async with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        route = respx_mock.post(f"{base}/api/v1/auth/ws-token").mock(
            side_effect=[httpx.Response(200, json={"token": name}) for name in ("t1", "t2")]
        )
        events = Events()
        sleeps = Sleeps(1)
        subject = live.LiveListener(
            HttpBambuddyClient(base, KEY, lambda: httpx.AsyncClient(verify=_TLS)), events, sleep=sleeps, now=Clock()
        )
        await run_until_stopped(subject)
    assert subject.state == "no_right"
    assert sleeps.delays == [live.NO_RIGHT_DELAY]
    assert route.call_count == 2
    assert events.kinds == []


# ---- the state reported to the caller (v0.3.6 p86) -------------------------------------------


def listener_with_states(connect, sleeps, states: list[str]) -> live.LiveListener:
    return live.LiveListener(
        quick_client(), Events(), connect=connect, sleep=sleeps, now=Clock(), on_state=states.append
    )


async def test_on_state_reports_each_change_once(respx_mock):
    tokens(respx_mock, "t1", "t2")
    states: list[str] = []
    subject = listener_with_states(FakeConnect(OSError("down"), OSError("down")), Sleeps(2), states)
    await run_until_stopped(subject)
    # Starting state "connecting" is not a change; a repeated state is not reported again. A second
    # attempt out of a pause stays "waiting": it used to say "connecting" and "waiting" again for every
    # attempt, two changes (and two reloads of open settings pages) for nothing (v0.3.8 p94).
    assert states == ["waiting"]


async def test_on_state_reports_connected_and_no_right(respx_mock):
    tokens(respx_mock, "t1")
    states: list[str] = []
    connect = FakeConnect([])
    subject = listener_with_states(connect, Sleeps(1), states)
    task = asyncio.create_task(subject.run())
    try:
        for _ in range(1000):
            await asyncio.sleep(0)
            if connect.connections and connect.connections[0].waiting.is_set():
                break
        assert states == ["connected"]
    finally:
        await stop(task)

    respx_mock.post(TOKEN_URL).respond(403, json={"detail": "Missing permission"})
    denied: list[str] = []
    await run_until_stopped(listener_with_states(FakeConnect(), Sleeps(1), denied))
    assert denied == ["no_right"]


async def test_a_failing_on_state_does_not_stop_the_listener(respx_mock):
    tokens(respx_mock, "t1")

    def broken(state: str) -> None:
        raise RuntimeError("boom")

    subject = live.LiveListener(
        quick_client(), Events(), connect=FakeConnect(OSError("down")), sleep=Sleeps(1), now=Clock(), on_state=broken
    )
    await run_until_stopped(subject)
    assert subject.state == "waiting"


# ---- waking the listener (v0.3.7 p97) -----------------------------------------------------
# A new key or new rights must not wait for the pause (up to 15 minutes): "check and save" in the
# settings wakes the listener, which tries again at once with a fresh token.


class HoldingSleep:
    """A pause that lasts until it is cancelled (the real 15 minutes): records each delay."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.Event().wait()


async def until(condition, *, steps: int = 2000) -> None:
    for _ in range(steps):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("did not happen in time")


@pytest.mark.parametrize(
    ("first", "fresh_token"),
    [([refused(403)] * 2, "t3"), ([OSError("down")], "t2")],
    ids=["no_right", "waiting"],
)
async def test_wake_ends_the_pause_and_tries_again_with_a_fresh_token(respx_mock, first, fresh_token):
    # no_right: t1 refused, t2 refused (the second try), pause; the wake fetches t3. waiting: t1, pause, t2.
    route = tokens(respx_mock, "t1", "t2", "t3")
    connect = FakeConnect(*first, [])
    sleeps = HoldingSleep()
    events = Events()
    subject = listener(connect, events, sleeps)
    task = asyncio.create_task(subject.run())
    try:
        await until(lambda: subject.state in ("no_right", "waiting") and len(sleeps.delays) == 1)
        assert events.kinds == []
        subject.wake()
        await until(lambda: subject.state == "connected")
        assert connect.urls[-1] == f"{WS}?token={fresh_token}"
        assert route.call_count == len(connect.urls)  # a token for every attempt: the old one is not reused
        assert events.kinds == ["connected"]
        assert len(sleeps.delays) == 1  # no second pause has begun
    finally:
        await stop(task)


async def test_wake_starts_the_pauses_over(respx_mock):
    # Without a wake the second pause is 15 s; after a wake the failures count from the start again.
    tokens(respx_mock, "t1", "t2", "t3")
    connect = FakeConnect(OSError("down"), OSError("down"), OSError("down"))
    sleeps = HoldingSleep()
    subject = listener(connect, Events(), sleeps)
    task = asyncio.create_task(subject.run())
    try:
        await until(lambda: len(sleeps.delays) == 1)
        subject.wake()
        await until(lambda: len(sleeps.delays) == 2)
        assert sleeps.delays == [5, 5]  # not [5, 15]
    finally:
        await stop(task)


async def test_wake_while_connected_changes_nothing_by_default(respx_mock):
    route = tokens(respx_mock, "t1", "t2")
    connect = FakeConnect([])
    subject = listener(connect, Events(), HoldingSleep())
    task = asyncio.create_task(subject.run())
    try:
        await until(lambda: connect.connections and connect.connections[0].waiting.is_set())
        subject.wake()
        for _ in range(50):
            await asyncio.sleep(0)
        assert subject.state == "connected"
        assert len(connect.connections) == 1 and not connect.connections[0].closed
        assert route.call_count == 1
    finally:
        await stop(task)


async def test_wake_with_reconnect_replaces_a_live_connection(respx_mock):
    # A new address or key: the connection that stands belongs to the old one.
    route = tokens(respx_mock, "t1", "t2")
    connect = FakeConnect([], [])
    events = Events()
    subject = listener(connect, events, HoldingSleep())
    task = asyncio.create_task(subject.run())
    try:
        await until(lambda: connect.connections and connect.connections[0].waiting.is_set())
        subject.wake(reconnect=True)
        await until(lambda: len(connect.connections) == 2 and connect.connections[1].waiting.is_set())
        assert connect.connections[0].closed
        assert connect.urls == [f"{WS}?token=t1", f"{WS}?token=t2"]
        assert route.call_count == 2
        assert subject.state == "connected"
        assert events.kinds == ["connected", "connected"]  # the poll after every connect
    finally:
        await stop(task)


async def test_a_wake_before_run_is_not_lost_and_does_no_harm(respx_mock):
    tokens(respx_mock, "t1")
    connect = FakeConnect([])
    subject = listener(connect, Events(), HoldingSleep())
    subject.wake(reconnect=True)
    task = asyncio.create_task(subject.run())
    try:
        await until(lambda: connect.connections and connect.connections[-1].waiting.is_set())
        assert subject.state == "connected"
    finally:
        await stop(task)


class SlowClose(FakeConnection):
    """A connection whose closing takes a while (the close handshake)."""

    async def __aexit__(self, *exc) -> None:
        self.closed = True
        await asyncio.sleep(0.2)


async def test_a_shutdown_during_a_restart_ends_run(respx_mock):
    # Review of v0.3.7: the cancel of the lifespan arrived while the old connection was closing and was
    # swallowed with the cancel of the connection; `run()` then connected again and the app hung.
    tokens(respx_mock, "t1", "t2")
    slow = SlowClose([])

    def connect(url, **options):
        return slow

    subject = listener(connect, Events(), HoldingSleep())
    task = asyncio.create_task(subject.run())
    await until(lambda: slow.waiting.is_set())
    subject.wake(reconnect=True)
    for _ in range(5):  # the old connection is closing now
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)


async def test_a_shutdown_while_the_pause_is_ended_ends_run(respx_mock):
    tokens(respx_mock, "t1", "t2")
    connect = FakeConnect(OSError("down"), [])
    subject = listener(connect, Events(), HoldingSleep())
    task = asyncio.create_task(subject.run())
    await until(lambda: subject.state == "waiting")
    subject.wake()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)


# ---- one change per state, not two per attempt (v0.3.8 p94) -------------------------------------------


async def test_many_failed_attempts_report_no_change_after_the_first(respx_mock):
    tokens(respx_mock, "t1", "t2", "t3", "t4")
    states: list[str] = []
    subject = listener_with_states(FakeConnect(*[OSError("down")] * 4), Sleeps(4), states)
    await run_until_stopped(subject)
    assert states == ["waiting"]


async def test_a_refusal_after_waiting_is_one_change_and_stays(respx_mock):
    tokens(respx_mock, "t1", "t2", "t3", "t4")
    states: list[str] = []
    connect = FakeConnect(OSError("down"), refused(403), refused(403), refused(403), refused(403))
    subject = listener_with_states(connect, Sleeps(3), states)
    await run_until_stopped(subject)
    assert states == ["waiting", "no_right"]  # not "connecting" in between


async def test_connected_then_lost_then_again_reports_each_real_change_once(respx_mock):
    tokens(respx_mock, "t1", "t2", "t3")
    states: list[str] = []
    connect = FakeConnect([closed(1006)], OSError("down"), [closed(1006)])
    subject = listener_with_states(connect, Sleeps(3), states)
    await run_until_stopped(subject)
    assert states == ["connected", "waiting", "connected", "waiting"]


async def test_after_a_wake_the_attempt_is_shown_as_connecting(respx_mock):
    """A person asked for the attempt (new key, new rights): the page says "connecting" for it."""
    tokens(respx_mock, "t1", "t2", "t3")
    states: list[str] = []
    sleeps = HoldingSleep()
    subject = listener_with_states(FakeConnect(OSError("down"), OSError("down")), sleeps, states)
    task = asyncio.create_task(subject.run())
    try:
        await until(lambda: len(sleeps.delays) == 1)
        assert states == ["waiting"]
        subject.wake()
        await until(lambda: len(sleeps.delays) == 2)
        assert states == ["waiting", "connecting", "waiting"]
    finally:
        await stop(task)


# ---- closing the channel properly (v0.3.8 p80) ------------------------------------------------------------


async def _serve_and_note_the_close_code(respx_mock, scenario):
    """Run the real library against a local server; `scenario(subject, task)` ends the connection.

    Returns the close code the server received (what Bambuddy would see).
    """
    codes: list[int | None] = []
    closed_seen = asyncio.Event()

    async def handler(connection):
        await connection.wait_closed()
        codes.append(connection.close_code)
        closed_seen.set()

    async with serve(handler, "127.0.0.1", 0, logger=quiet_server_log()) as server:
        port = server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        respx_mock.post(f"{base}/api/v1/auth/ws-token").respond(json={"token": "t1"})
        subject = live.LiveListener(
            HttpBambuddyClient(base, KEY, lambda: httpx.AsyncClient(verify=_TLS)), Events()
        )
        task = asyncio.create_task(subject.run())
        try:
            await asyncio.wait_for(until_connected(subject), timeout=5)
            await scenario(subject, task)
            await asyncio.wait_for(closed_seen.wait(), timeout=5)
        finally:
            await stop(task)
    return codes[0]


async def until_connected(subject) -> None:
    while subject.state != "connected":
        await asyncio.sleep(0.01)


async def test_shutting_down_closes_the_connection_as_finished_not_as_an_error(respx_mock):
    async def shutdown(subject, task):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert await _serve_and_note_the_close_code(respx_mock, shutdown) == 1000


async def test_a_reconnect_closes_the_old_connection_as_finished_too(respx_mock):
    async def reconnect(subject, task):
        subject.wake(reconnect=True)

    assert await _serve_and_note_the_close_code(respx_mock, reconnect) == 1000


async def test_a_silent_bambuddy_does_not_hold_the_shutdown_longer_than_the_close_time(respx_mock, monkeypatch):
    """Review: the library waits for the answer to its close for 10 s by default; here it is CLOSE_WAIT."""
    monkeypatch.setattr(live, "CLOSE_WAIT", 0.3)
    connected = asyncio.Event()
    finished = asyncio.Event()

    async def handler(connection):
        connection.transport.pause_reading()  # takes in nothing more: the close is never answered
        connected.set()
        await finished.wait()

    async with serve(handler, "127.0.0.1", 0, logger=quiet_server_log(), close_timeout=0.1) as server:
        port = server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        respx_mock.post(f"{base}/api/v1/auth/ws-token").respond(json={"token": "t1"})
        subject = live.LiveListener(
            HttpBambuddyClient(base, KEY, lambda: httpx.AsyncClient(verify=_TLS)), Events()
        )
        task = asyncio.create_task(subject.run())
        try:
            await asyncio.wait_for(connected.wait(), timeout=5)
            await asyncio.wait_for(until_connected(subject), timeout=5)
            began = asyncio.get_running_loop().time()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            assert asyncio.get_running_loop().time() - began < 3  # not the library's 10 s
            assert task.done()
        finally:
            finished.set()
            await stop(task)
