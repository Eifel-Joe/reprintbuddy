"""Protection against hashing floods and password guessing.

argon2 is deliberately expensive in time and memory. Without a limit, a few dozen
simultaneous login attempts would occupy every worker thread and a lot of memory;
without a brake, guessing would be limited only by the speed of the network.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from collections import deque
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool


class HashLimiter:
    """Runs CPU-heavy calls in worker threads, at most `capacity` at a time.

    Waiting calls wait in the event loop and do not occupy a thread or a database
    connection. (One semaphore per event loop: tests start a new loop per request.)
    """

    def __init__(self, capacity: int = 2):
        self.capacity = capacity
        self._semaphores: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        semaphore = self._semaphores.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.capacity)
            self._semaphores[loop] = semaphore
        return semaphore

    async def run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        async with self._semaphore():
            return await run_in_threadpool(func, *args, **kwargs)


class FailureBrake:
    """A global brake on logins: with `threshold` failures within `window` seconds,
    every further login attempt is delayed by `delay` seconds.

    It is global, not per address: behind a proxy all requests share one address,
    and a service on a local network has one account to protect anyway.
    """

    def __init__(
        self,
        threshold: int = 10,
        window: float = 60.0,
        delay: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.threshold = threshold
        self.window = window
        self.delay = delay
        self.clock = clock
        self.failures: deque[float] = deque(maxlen=1000)

    def record_failure(self) -> None:
        self.failures.append(self.clock())

    def current_delay(self) -> float:
        cutoff = self.clock() - self.window
        while self.failures and self.failures[0] < cutoff:
            self.failures.popleft()
        return self.delay if len(self.failures) >= self.threshold else 0.0
