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


def _warnings_only(record: logging.LogRecord) -> bool:
    return record.levelno >= logging.WARNING


# The library's own log for this connection. At DEBUG it writes the handshake, token included
# (`> GET /api/v1/ws?token=…`, websockets `client.py`), and its INFO lines are of no use here.
# So it gets a logger of its own that lets nothing below WARNING out: by its level, and by a
# filter in case a logging setup turns the level down. Its WARNING and ERROR lines carry no
# address (websockets 17: keepalive, parser and internal errors).
LIBRARY_LOG = logging.getLogger(f"{__name__}.websockets")
LIBRARY_LOG.setLevel(logging.WARNING)
LIBRARY_LOG.addFilter(_warnings_only)

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
    `"no_right"` (Bambuddy refuses the channel; polling goes on without it). `on_state(state)`
    is called after each change of it (not for the starting state, not for a repeat), so a page
    that shows the state can follow it; it must be short and may not raise (an error is logged).
    """

    def __init__(
        self,
        client: LiveClient,
        on_event: OnEvent,
        *,
        connect: Callable[[str], Any] = connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.monotonic,
        on_state: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._on_event = on_event
        self._connect = connect
        self._sleep = sleep
        self._now = now
        self._on_state = on_state
        self._state = "connecting"
        self._token: str | None = None
        self._token_at = 0.0
        self._failures = 0  # failed attempts since the last stable connection
        self._paused_in: str | None = None  # the state of the last pause; None after a connect

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        if value == self._state:
            return
        self._state = value
        if self._on_state is not None:
            try:
                self._on_state(value)
            except Exception:  # the page missing a change must not stop the channel
                log.warning("Bambuddy live channel: reporting the state %r failed", value, exc_info=True)

    async def run(self) -> None:
        """Connect, listen, pause, again; only `CancelledError` leaves."""
        while True:
            delay = await self._cycle()
            await self._sleep(delay)

    async def _cycle(self) -> float:
        """One attempt (two after a refusal); the pause to take afterwards."""
        self.state = "connecting"
        try:
            outcome, reason = await self._connect_once()
            if outcome == _REFUSED:
                # The token may be expired or revoked: one more try with a fresh one.
                self._token = None
                outcome, reason = await self._connect_once()
                if outcome == _REFUSED:
                    self._token = None
                    return self._no_right(reason)
            return self._retry(reason)
        except BambuddyRejected as exc:
            if exc.status == 403:
                return self._no_right("token refused with HTTP 403")
            return self._retry(f"token refused with HTTP {exc.status}")
        except Exception as exc:  # network, Bambuddy unavailable, anything else: try later
            # Only the type: a message may hold the address with its token (`InvalidURI`).
            return self._retry(type(exc).__name__)

    async def _connect_once(self) -> tuple[str, str]:
        """Connect and listen until the connection ends; (`_REFUSED` or `_LOST`, reason)."""
        url = self._client.ws_url(await self._current_token())
        address = _without_query(url)
        opened: float | None = None
        try:
            async with self._connect(url, logger=LIBRARY_LOG) as connection:
                opened = self._now()
                self.state = "connected"
                self._paused_in = None
                log.info("Bambuddy live channel connected: %s", address)
                await self._emit("connected")
                while True:
                    kind = _kind(await connection.recv())
                    if kind in TRIGGER_TYPES:
                        await self._emit(kind)
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd is not None else None
            log.debug("Bambuddy live channel closed (%s): %s", code, address)
            return (_REFUSED if code == UNAUTHORIZED_CLOSE else _LOST), f"closed with code {code}"
        except InvalidStatus as exc:
            status = exc.response.status_code
            log.debug("Bambuddy live channel handshake answered HTTP %s: %s", status, address)
            refused = status in REFUSED_HANDSHAKE
            return (_REFUSED if refused else _LOST), f"handshake answered HTTP {status}"
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

    def _retry(self, reason: str) -> float:
        delay = RETRY_DELAYS[min(self._failures, len(RETRY_DELAYS) - 1)]
        self._failures += 1
        self._pause("waiting", "Bambuddy live channel lost (%s); next attempt in %s s", reason, delay)
        return delay

    def _no_right(self, reason: str) -> float:
        self._failures = 0  # after the pause, everything starts over
        # A 403 can also come from Bambuddy failing to check the right, or from a proxy.
        self._pause(
            "no_right",
            "Bambuddy live channel refused (%s): probably the key's group lacks"
            " websocket:connect; next attempt in %s s",
            reason,
            NO_RIGHT_DELAY,
        )
        return NO_RIGHT_DELAY

    def _pause(self, state: str, message: str, *args: object) -> None:
        """Go into `state`; a WARNING when that is a change, DEBUG while it repeats."""
        level = logging.DEBUG if self._paused_in == state else logging.WARNING
        self._paused_in = state
        self.state = state
        log.log(level, message, *args)
