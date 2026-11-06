"""A real server with an open live stream stops within the grace time."""

import threading
import time

import httpx
import uvicorn

COOKIE = "reprintbuddy_session"
GRACE_SECONDS = 1


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
        with httpx.Client(timeout=5, cookies=cookies) as http:
            with http.stream("GET", f"http://127.0.0.1:{port}/live") as response:
                assert response.status_code == 200
                lines = response.iter_lines()
                for line in lines:
                    if line == "event: hello":
                        break
                else:
                    raise AssertionError("the stream ended before `event: hello`")

                server.should_exit = True
                thread.join(timeout=GRACE_SECONDS + 3)
                assert not thread.is_alive(), "the server did not stop while a stream was open"

                try:  # the stream must end now: cleanly or aborted
                    for _ in lines:
                        pass
                except (httpx.RemoteProtocolError, httpx.ReadError):
                    pass
    finally:
        server.should_exit = True
        thread.join(timeout=5)
