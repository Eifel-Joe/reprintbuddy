"""In-memory broker for change notices.

Publishers (the change feed) may run on any thread; subscribers live on an
asyncio loop. The broker carries topics only, never contents.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from collections.abc import Iterable


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
