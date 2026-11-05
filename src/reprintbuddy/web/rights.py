"""Does the stored Bambuddy key still have every right this version needs?

A new version of Reprintbuddy may need a right the key does not have yet (Spec 11.3,
"Rights after an update"). The monitor holds the result of the last check in memory, once
per process, so once per start. The first page after a start waits for the answer, but at
most `FIRST_WAIT_SECONDS`; every later page only starts a new attempt in the background
and shows what is known by then. Bambuddy being down never blocks a page for longer.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Literal

import httpx

from reprintbuddy.bambuddy.connection import ConnectionCheck, check_connection

FIRST_WAIT_SECONDS = 3.0

Status = Literal["unknown", "ok", "missing"]


class RightsMonitor:
    def __init__(self) -> None:
        self.status: Status = "unknown"
        self.check: ConnectionCheck | None = None
        self.attempts = 0
        self._task: asyncio.Task[None] | None = None  # kept so it is not collected while it runs
        self._first_deadline: float | None = None  # until when the first page may wait

    def record(self, check: ConnectionCheck) -> None:
        """Take over the result of a check made anywhere (this monitor, settings, setup).

        Only a verdict about the rights counts. An unreachable Bambuddy or a refused key
        says nothing about the rights and leaves what is known as it is.
        """
        if check.ok:
            self.status, self.check = "ok", None
        elif check.reason == "missing_permissions":
            self.status, self.check = "missing", check

    @property
    def notice(self) -> ConnectionCheck | None:
        return self.check if self.status == "missing" else None

    async def ensure(
        self,
        read_credentials: Callable[[], tuple[str, str] | None],
        client_factory: Callable[[], httpx.AsyncClient],
    ) -> None:
        """Make sure a verdict is there or on its way.

        With a verdict nothing happens. Without one a check runs in the background, and
        only the first attempt after the start is waited for (at most `FIRST_WAIT_SECONDS`).
        """
        if self.status != "unknown":
            return
        loop = asyncio.get_running_loop()
        if self._task is None or self._task.done():
            credentials = read_credentials()  # reads and closes its own database session
            if credentials is None:
                return  # setup not finished: nothing to check yet
            self.attempts += 1
            self._task = asyncio.create_task(self._run(credentials, client_factory))
            if self.attempts == 1:
                self._first_deadline = loop.time() + FIRST_WAIT_SECONDS
        if self.attempts != 1 or self._first_deadline is None:
            return
        remaining = self._first_deadline - loop.time()
        if remaining > 0:
            await asyncio.wait({self._task}, timeout=remaining)

    async def _run(
        self, credentials: tuple[str, str], client_factory: Callable[[], httpx.AsyncClient]
    ) -> None:
        url, api_key = credentials
        try:
            async with client_factory() as client:
                check = await check_connection(url, api_key, client)
        except Exception:  # never let a failed check break a page; the next call tries again
            return
        self.record(check)
