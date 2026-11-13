"""A real server with an open live stream stops within the grace time.

p451 (v0.4.5): once in about 130 CI runs (Python 3.14, Linux) the server thread still ran 4 s after the
stop. The log showed that the grace had worked: Uvicorn cancelled the stream after it and closed the
connection. What took the rest of the time did not show, and it never happened locally (54 runs, also
under load, all at about 1.2 s). So the test now checks the two steps apart: the stream must end within
the grace plus a little (that is what the grace is for, and stricter than before), the thread may take
longer to tear its event loop down, but must still end long before systemd would kill the service
(90 s). A step that overruns puts the stack of the server thread into the message, so a next failure
names its cause.

p535 (v0.4.8): it failed once more on the tag v0.4.7 (Python 3.13, Linux), "the stream stayed open 3 s
after the stop", the server thread inside `logger.error` reading source lines for a traceback. Root:
Uvicorn tells a running response nothing when it stops (`H11Protocol.shutdown` only drops keep-alive),
so the stream ran into the grace and was cancelled; uvicorn then logs the cancelled request with its
whole traceback and closes the connection only after that (`h11_impl.run_asgi`). How long the stream
stayed open hung on how fast the log was written. Reproduced at will with an error log that takes
3.5 s: exactly the CI message. The server now closes the live channel first (`web/server.py`), so the
streams end themselves, nothing is cancelled and nothing is logged.
"""

import logging
import sys
import threading
import time
import traceback

import httpx
import uvicorn

from reprintbuddy.web.server import LiveClosingServer

COOKIE = "reprintbuddy_session"
GRACE_SECONDS = 1
STREAM_ENDS_WITHIN = GRACE_SECONDS + 2  # the grace, Uvicorn's 0.1 s steps, a slow machine
THREAD_ENDS_WITHIN = GRACE_SECONDS + 10  # plus tearing the event loop down; systemd waits 90 s


def _stack(thread: threading.Thread) -> str:
    frame = sys._current_frames().get(thread.ident) if thread.ident is not None else None
    if frame is None:
        return "(the server thread has ended)"
    return "".join(traceback.format_stack(frame))


class _Errors(logging.Handler):
    """What uvicorn logs at ERROR while it stops; `delay` stands for a slow disk or a slow machine."""

    def __init__(self, delay: float = 0.0) -> None:
        super().__init__(level=logging.ERROR)
        self.delay = delay
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage().splitlines()[0])
        if record.exc_info and self.delay:
            time.sleep(self.delay)


def _stop_with_an_open_stream(ready_client, errors: _Errors) -> None:
    config = uvicorn.Config(
        ready_client.app,
        host="127.0.0.1",
        port=0,
        timeout_graceful_shutdown=GRACE_SECONDS,
        log_level="warning",
        lifespan="off",
    )
    # After the Config: it sets uvicorn's log up anew and would drop the handler.
    logging.getLogger("uvicorn.error").addHandler(errors)
    try:
        _run(ready_client, LiveClosingServer(config, live=ready_client.app.state.live))
    finally:
        logging.getLogger("uvicorn.error").removeHandler(errors)


def test_server_with_an_open_stream_stops_within_the_grace_time(ready_client):
    errors = _Errors()
    _stop_with_an_open_stream(ready_client, errors)
    # The stream ends by itself: uvicorn neither cancels it nor logs it as a failed request.
    assert errors.messages == []


def test_a_slow_error_log_does_not_keep_the_stream_open(ready_client):
    """p535: the CI failure, at will. The stream must not wait for uvicorn's error log."""
    errors = _Errors(delay=STREAM_ENDS_WITHIN + 0.5)
    _stop_with_an_open_stream(ready_client, errors)
    assert errors.messages == []


def _run(ready_client, server: uvicorn.Server) -> None:
    # Outside the main thread Uvicorn installs no signal handlers.
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "the server did not start within 5 seconds"
        port = server.servers[0].sockets[0].getsockname()[1]
        cookies = {COOKIE: ready_client.cookies.get(COOKIE)}
        # A read waits at most until the stream must have ended; a server that keeps it open fails the read.
        timeout = httpx.Timeout(5, read=STREAM_ENDS_WITHIN)
        with httpx.Client(timeout=timeout, cookies=cookies) as http:
            with http.stream("GET", f"http://127.0.0.1:{port}/live") as response:
                assert response.status_code == 200
                # Kept: a dropped iterator would close the connection from this side (p451 measurement).
                lines = response.iter_lines()
                for line in lines:
                    if line == "event: hello":
                        break
                else:
                    raise AssertionError("the stream ended before `event: hello`")

                stopped_at = time.monotonic()
                server.should_exit = True
                try:  # the stream must end now, cleanly: the server ends it, uvicorn does not cut it off (p535)
                    for _ in lines:
                        pass
                except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
                    raise AssertionError(f"the stream was cut off instead of ending: {exc!r}") from None
                except httpx.ReadTimeout:
                    raise AssertionError(
                        f"the stream stayed open {STREAM_ENDS_WITHIN} s after the stop; server thread:\n{_stack(thread)}"
                    ) from None
                ended = time.monotonic() - stopped_at
                assert ended <= STREAM_ENDS_WITHIN, (
                    f"the stream ended only {ended:.2f} s after the stop; server thread:\n{_stack(thread)}"
                )

        thread.join(timeout=max(0.0, stopped_at + THREAD_ENDS_WITHIN - time.monotonic()))
        assert not thread.is_alive(), (
            f"the server did not stop within {THREAD_ENDS_WITHIN} s while a stream was open; "
            f"server thread:\n{_stack(thread)}"
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
