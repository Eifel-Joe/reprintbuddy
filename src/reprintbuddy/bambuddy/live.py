"""Bambuddy's live channel, client side (Spec 7.6, B2 Task 9).

`LiveListener` holds one websocket to Bambuddy (`/api/v1/ws?token=…`) and tells its caller
when a print starts or ends or a queue entry is taken or fails. It only listens: it sends
nothing over the socket (the library keeps the connection alive with protocol pings), and it
asks the client for nothing but the token and the address. What the caller does with an
event (fetch the queue at once) is B4's business.

Bambuddy's facts, from its source (to be confirmed in the user's live test):

- `POST /auth/ws-token` needs `websocket:connect`; without it the answer is 403. The token
  lives 60 minutes and is checked only when connecting.
- An invalid or expired token, or a missing right, closes the socket with 4401 before it is
  accepted. Bambuddy runs under uvicorn, which turns a close before `accept()` into an HTTP
  403 answer to the handshake; the client then sees that, not the close code. Both count as
  a refusal here.
- There is no message "queue changed"; `TRIGGER_TYPES` are the messages after which the
  queue may look different.

Neither the token nor the key is ever logged: the address is logged without its query, and
errors only by their type.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from reprintbuddy.bambuddy.client import BambuddyRejected

log = logging.getLogger(__name__)

# Messages after which the queue may have changed. `queue_item_*` go to the entry's user
# only; a socket opened with a key carries no user, so they probably never arrive.
TRIGGER_TYPES = frozenset({"print_start", "print_complete", "queue_item_acked", "queue_item_failed"})

TOKEN_MAX_AGE = 55 * 60  # seconds; Bambuddy's token lives 60 minutes, 5 minutes to spare
RETRY_DELAYS = (5, 15, 60, 300)  # seconds between attempts; then always the last one
NO_RIGHT_DELAY = 900  # seconds of pause when the key's group lacks `websocket:connect`
STABLE_AFTER = 60  # seconds a connection must hold before the pauses start over

UNAUTHORIZED_CLOSE = 4401  # Bambuddy's close code for a refused token
REFUSED_HANDSHAKE = frozenset({401, 403})  # the same refusal as uvicorn answers it

_REFUSED = "refused"
_LOST = "lost"


class LiveClient(Protocol):
    """What the listener needs of the client (`HttpBambuddyClient` has both)."""

    async def ws_token(self) -> str: ...

    def ws_url(self, token: str) -> str: ...


OnEvent = Callable[[str], "Awaitable[None] | None"]


def _without_query(url: str) -> str:
    """The address for a log line: no query (so no token), no user part."""
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return f"{parts.scheme}://{host}{parts.path}"


def _kind(message: Any) -> str | None:
    """The `type` of a message that is a JSON object, else None."""
    try:
        data = json.loads(message)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("type")
    return kind if isinstance(kind, str) else None


class LiveListener:
    """Listens to Bambuddy's live channel until cancelled.

    `on_event(kind)` (plain or `async`) is called with the `type` of every message in
    `TRIGGER_TYPES`, and with `"connected"` after every successful connect: messages from the
    time without a connection are lost, so the caller should look once.

    `state` is `"connecting"`, `"connected"`, `"waiting"` (a pause after a failure) or
    `"no_right"` (Bambuddy refuses the channel; polling goes on without it).
    """

    def __init__(
        self,
        client: LiveClient,
        on_event: OnEvent,
        *,
        connect: Callable[[str], Any] = connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._on_event = on_event
        self._connect = connect
        self._sleep = sleep
        self._now = now
        self.state = "connecting"
        self._token: str | None = None
        self._token_at = 0.0
        self._failures = 0  # failed attempts since the last stable connection

    async def run(self) -> None:
        """Connect, listen, pause, again; only `CancelledError` leaves."""
        while True:
            delay = await self._cycle()
            await self._sleep(delay)

    async def _cycle(self) -> float:
        """One attempt (two after a refusal); the pause to take afterwards."""
        self.state = "connecting"
        try:
            outcome = await self._connect_once()
            if outcome == _REFUSED:
                # The token may be expired or revoked: one more try with a fresh one.
                self._token = None
                outcome = await self._connect_once()
                if outcome == _REFUSED:
                    self._token = None
                    return self._no_right()
            return self._retry()
        except BambuddyRejected as exc:
            if exc.status == 403:
                return self._no_right()
            log.warning("Bambuddy live channel: token refused with HTTP %s", exc.status)
            return self._retry()
        except Exception as exc:  # network, Bambuddy unavailable, anything else: try later
            log.warning("Bambuddy live channel: %s", type(exc).__name__)
            return self._retry()

    async def _connect_once(self) -> str:
        """Connect and listen until the connection ends; `_REFUSED` or `_LOST`."""
        url = self._client.ws_url(await self._current_token())
        address = _without_query(url)
        opened: float | None = None
        try:
            async with self._connect(url) as connection:
                opened = self._now()
                self.state = "connected"
                log.info("Bambuddy live channel connected: %s", address)
                await self._emit("connected")
                while True:
                    kind = _kind(await connection.recv())
                    if kind in TRIGGER_TYPES:
                        await self._emit(kind)
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd is not None else None
            log.info("Bambuddy live channel closed (%s): %s", code, address)
            if code == UNAUTHORIZED_CLOSE:
                return _REFUSED
            return _LOST
        except InvalidStatus as exc:
            status = exc.response.status_code
            log.info("Bambuddy live channel refused with HTTP %s: %s", status, address)
            if status in REFUSED_HANDSHAKE:
                return _REFUSED
            return _LOST
        finally:
            if opened is not None and self._now() - opened >= STABLE_AFTER:
                self._failures = 0

    async def _current_token(self) -> str:
        """The cached token while it is young enough, else a fresh one."""
        if self._token is None or self._now() - self._token_at >= TOKEN_MAX_AGE:
            self._token = None
            fetched_at = self._now()
            self._token = await self._client.ws_token()
            self._token_at = fetched_at
        return self._token

    async def _emit(self, kind: str) -> None:
        try:
            result = self._on_event(kind)
            if inspect.isawaitable(result):
                await result
        except Exception:
            log.exception("Bambuddy live channel: handling %r failed", kind)

    def _retry(self) -> float:
        self.state = "waiting"
        delay = RETRY_DELAYS[min(self._failures, len(RETRY_DELAYS) - 1)]
        self._failures += 1
        log.info("Bambuddy live channel: next attempt in %s s", delay)
        return delay

    def _no_right(self) -> float:
        self.state = "no_right"
        self._failures = 0  # after the pause, everything starts over
        log.warning(
            "Bambuddy live channel refused: the key's group needs websocket:connect;"
            " next attempt in %s s",
            NO_RIGHT_DELAY,
        )
        return NO_RIGHT_DELAY
