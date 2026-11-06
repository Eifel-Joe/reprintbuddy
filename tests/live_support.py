"""Drive an endless server-sent event stream in tests.

Starlette's `TestClient` and `httpx.ASGITransport` collect the whole response before they
return, so with an endless stream they never return. `open_stream` talks to the
application over ASGI itself: the scope looks like Uvicorn's h11 one (ASGI spec 2.3, so
a streaming response listens for `http.disconnect`), and `receive` answers with the empty
request body first and with `http.disconnect` only after `disconnect()`.
"""

from __future__ import annotations

import asyncio
import json

COOKIE = "reprintbuddy_session"


class StreamProbe:
    def __init__(self, app, path: str, cookie: str | None) -> None:
        raw_path, _, query = path.partition("?")
        headers = [(b"host", b"testserver"), (b"accept", b"text/event-stream")]
        if cookie:
            headers.append((b"cookie", f"{COOKIE}={cookie}".encode()))
        self._scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
            "state": {},
        }
        self._app = app
        self._body_sent = False
        self._disconnected = asyncio.Event()
        self._messages: asyncio.Queue[dict] = asyncio.Queue()
        self._buffer = ""
        self._ended = False
        self.status: int = 0
        self.headers: dict[str, str] = {}
        self.retry: int | None = None
        self._task: asyncio.Task | None = None

    async def _receive(self) -> dict:
        if not self._body_sent:
            self._body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnected.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict) -> None:
        await self._messages.put(message)

    async def _start(self, timeout: float) -> None:
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        message = await asyncio.wait_for(self._messages.get(), timeout)
        assert message["type"] == "http.response.start", message
        self.status = message["status"]
        for name, value in message.get("headers", []):
            key = name.decode("latin-1").lower()
            # Several Set-Cookie lines would be folded; for the tests one is enough to see.
            self.headers[key] = value.decode("latin-1")

    async def _read_block(self, timeout: float) -> str | None:
        """The next block up to an empty line; None when the body has ended."""
        deadline = asyncio.get_running_loop().time() + timeout
        while "\n\n" not in self._buffer:
            if self._ended:
                return None
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("no event in time")
            message = await asyncio.wait_for(self._messages.get(), remaining)
            assert message["type"] == "http.response.body", message
            self._buffer += message.get("body", b"").decode("utf-8")
            if not message.get("more_body", False):
                self._ended = True
        block, self._buffer = self._buffer.split("\n\n", 1)
        return block

    async def next_event(self, timeout: float = 5.0) -> tuple[str, dict | str]:
        """The next event: ("hello", {...}), ("change", {...}), ("comment", "ping") ...

        A block with only `retry:` is noted in `self.retry` and skipped.
        """
        while True:
            block = await self._read_block(timeout)
            if block is None:
                raise EOFError("the stream has ended")
            lines = block.split("\n")
            if all(line.startswith(":") for line in lines):
                return "comment", "\n".join(line[1:].strip() for line in lines)
            fields: dict[str, list[str]] = {}
            for line in lines:
                name, _, value = line.partition(":")
                fields.setdefault(name, []).append(value[1:] if value.startswith(" ") else value)
            if set(fields) == {"retry"}:
                self.retry = int(fields["retry"][0])
                continue
            event = fields.get("event", ["message"])[0]
            data = "\n".join(fields.get("data", []))
            try:
                return event, json.loads(data)
            except ValueError:
                return event, data

    async def wait_closed(self, timeout: float = 5.0) -> None:
        """Wait for the end of the response (the application returned)."""
        assert self._task is not None
        await asyncio.wait_for(asyncio.shield(self._task), timeout)
        while not self._messages.empty():
            message = self._messages.get_nowait()
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                self._ended = True

    async def disconnect(self) -> None:
        """The browser goes away: `receive` answers with `http.disconnect` from now on."""
        self._disconnected.set()
        await asyncio.sleep(0)

    async def aclose(self) -> None:
        """Clean up after a test: disconnect and wait briefly, then cancel."""
        if self._task is None or self._task.done():
            return
        self._disconnected.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), 2.0)
        except (TimeoutError, asyncio.TimeoutError):
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass


async def open_stream(app, path: str = "/live", *, cookie: str | None = None) -> StreamProbe:
    """Start a GET on `path` and return once the response has started."""
    probe = StreamProbe(app, path, cookie)
    await probe._start(timeout=5.0)
    return probe
