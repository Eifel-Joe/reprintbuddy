"""In-memory broker for change notices, and the stream that carries them to the pages.

Publishers (the change feed) may run on any thread; subscribers live on an
asyncio loop. The broker carries topics only, never contents.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from collections.abc import AsyncIterator, Iterable

LIVE_CHANNEL_TOPIC = "live-channel"  # the state of Bambuddy's live channel changed (not a stored change)
RETRY_MS = 2000  # the browser waits this long before it connects again
HEARTBEAT_SECONDS = 15.0  # a comment keeps proxies from closing a quiet stream
COALESCE_SECONDS = 0.25  # notices that come in this soon after one another go out as one
MAX_STREAM_SECONDS = 600.0  # then the stream ends; the browser reconnects and the gate checks again


class BrokerClosed(Exception):
    """The broker was closed; the subscription ends."""


class Subscription:
    def __init__(self, broker: LiveBroker, loop: asyncio.AbstractEventLoop) -> None:
        self._broker = broker
        self._loop = loop
        self._event = asyncio.Event()
        self._pending: set[str] = set()  # guarded by broker._lock

    def _wake(self) -> None:
        # Called with the broker lock held, from any thread. Raises
        # RuntimeError if the loop is already closed.
        self._loop.call_soon_threadsafe(self._event.set)

    async def wait(self, timeout: float) -> frozenset[str] | None:
        """Union of all topics since the last call; None after `timeout`."""
        deadline = time.monotonic() + timeout
        while True:
            with self._broker._lock:
                if self._broker._closed:
                    raise BrokerClosed
                self._event.clear()
                if self._pending:
                    topics = frozenset(self._pending)
                    self._pending.clear()
                    return topics
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._event.wait(), remaining)
            except TimeoutError:
                return None

    def close(self) -> None:
        self._broker._unsubscribe(self)


class LiveBroker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[Subscription] = []
        self._closed = False
        self._start = secrets.token_hex(4)
        self._counter = 0

    @property
    def state(self) -> str:
        with self._lock:
            return f"{self._start}:{self._counter}"

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def publish(self, topics: Iterable[str]) -> None:
        topics = frozenset(topics)
        if not topics:
            return
        with self._lock:
            self._counter += 1
            for sub in list(self._subs):
                sub._pending |= topics
                try:
                    sub._wake()
                except RuntimeError:  # its loop is closed
                    self._subs.remove(sub)

    def subscribe(self) -> Subscription:
        sub = Subscription(self, asyncio.get_running_loop())
        with self._lock:
            if not self._closed:
                self._subs.append(sub)
        return sub

    def close(self) -> None:
        with self._lock:
            self._closed = True
            subs, self._subs = self._subs, []
            for sub in subs:
                try:
                    sub._wake()
                except RuntimeError:
                    pass

    def _unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)


def _event(name: str, data: dict[str, object]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def event_stream(
    broker: LiveBroker,
    *,
    version: str,
    page_version: str | None,
    heartbeat: float,
    coalesce: float,
    lifetime: float,
) -> AsyncIterator[str]:
    """The events of one open page: `hello` (or `reload` for a page of another version),
    then `change` for every saved change and a comment while nothing happens.

    It subscribes before it says hello, so nothing saved after the `hello` state is missed.
    """
    sub = broker.subscribe()
    try:
        yield f"retry: {RETRY_MS}\n\n"
        if page_version is not None and page_version != version:
            yield _event("reload", {"version": version})
            return
        yield _event("hello", {"state": broker.state, "version": version})
        deadline = time.monotonic() + lifetime
        while (remaining := deadline - time.monotonic()) > 0:
            topics = await sub.wait(min(heartbeat, remaining))
            if topics is None:
                if time.monotonic() < deadline:
                    yield ": ping\n\n"
                continue
            await asyncio.sleep(coalesce)
            stragglers = await sub.wait(0)
            if stragglers:
                topics = topics | stragglers
            yield _event("change", {"state": broker.state, "topics": sorted(topics)})
    except BrokerClosed:
        return
    finally:
        # Only after the loop has stopped waiting: closing does not wake a pending wait.
        sub.close()
