"""How `reprintbuddy serve` runs uvicorn: like `uvicorn.run`, with a server that ends the live channel
before it waits for the open requests (p535, v0.4.8).

Root: uvicorn tells a running response nothing when it stops; `H11Protocol.shutdown` only drops the
keep-alive of a request still running. An open page's live stream (`GET /live`, `web/live.py`) never
ends by itself, so it always ran into the grace (`timeout_graceful_shutdown`, cli.py) and was
cancelled. After a cancel uvicorn first writes the failed request with its whole traceback to the log,
reading every source line from disk, and only then closes the connection (`h11_impl.run_asgi`). How
long a stream stayed open after the stop hung on how fast that log was written: on a CI machine once
more than the 2 s the test allows (tag v0.4.7, server thread in `logger.error` → `linecache`).

Fix: on the stop the server first closes the live broker. Every open stream then ends at once through
`BrokerClosed`, the response is complete, and uvicorn closes the connection as for any finished
request: nothing is cancelled, nothing is logged. The grace stays as the net for other requests.

Not chosen: a longer grace or test limit (the end would still hang on the log); plain ASGI instead of
the four `BaseHTTPMiddleware` layers (a shorter traceback, but the end would still wait for the log);
the lifespan shutdown (uvicorn sends it only after it has waited for the requests).
Tests: tests/web/test_live_shutdown.py, tests/web/test_live_server.py.
"""

from __future__ import annotations

import socket
import sys
from typing import Any

import uvicorn
from uvicorn.main import STARTUP_FAILURE

from reprintbuddy.web.live import LiveBroker


class LiveClosingServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, live: LiveBroker) -> None:
        super().__init__(config)
        self.live = live

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        # First: the open streams end now, before uvicorn starts waiting for the requests.
        self.live.close()
        await super().shutdown(sockets)


def serve(app: Any, **options: Any) -> None:
    """`uvicorn.run(app, **options)` for one process, with `LiveClosingServer`."""
    server = LiveClosingServer(uvicorn.Config(app, **options), live=app.state.live)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    if not server.started:
        sys.exit(STARTUP_FAILURE)
