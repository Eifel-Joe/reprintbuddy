"""A real server with an open live stream stops within the grace time.

p451 (v0.4.5): once in about 130 CI runs (Python 3.14, Linux) the server thread still ran 4 s after the
stop. The log showed that the grace had worked: Uvicorn cancelled the stream after it and closed the
connection. What took the rest of the time did not show, and it never happened locally (54 runs, also
under load, all at about 1.2 s). So the test now checks the two steps apart: the stream must end within
the grace plus a little (that is what the grace is for, and stricter than before), the thread may take
longer to tear its event loop down, but must still end long before systemd would kill the service
(90 s). A step that overruns puts the stack of the server thread into the message, so a next failure
names its cause.
"""

import sys
import threading
import time
import traceback

import httpx
import uvicorn

COOKIE = "reprintbuddy_session"
GRACE_SECONDS = 1
STREAM_ENDS_WITHIN = GRACE_SECONDS + 2  # the grace, Uvicorn's 0.1 s steps, a slow machine
THREAD_ENDS_WITHIN = GRACE_SECONDS + 10  # plus tearing the event loop down; systemd waits 90 s


def _stack(thread: threading.Thread) -> str:
    frame = sys._current_frames().get(thread.ident) if thread.ident is not None else None
    if frame is None:
        return "(the server thread has ended)"
    return "".join(traceback.format_stack(frame))


def test_server_with_an_open_stream_stops_within_the_grace_time(ready_client):
    config = uvicorn.Config(
        ready_client.app,
        host="127.0.0.1",
        port=0,
        timeout_graceful_shutdown=GRACE_SECONDS,
        log_level="warning",
        lifespan="off",
    )
    server = uvicorn.Server(config)
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
                try:  # the stream must end now: cleanly or aborted
                    for _ in lines:
                        pass
                except (httpx.RemoteProtocolError, httpx.ReadError):
                    pass
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
