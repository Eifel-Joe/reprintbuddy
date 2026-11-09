"""The automatic check for new versions (v0.3.6, Spec 11.3a).

Reprintbuddy asks at the start and then once a day whether a newer release exists (the same
check as the button "Check for updates": `versions.check_versions`). The loop knows nothing of
the web layer or of the clock; the app hands in what it needs (see `web/app.py`).

- The check runs git and may wait for the network (up to 20 s), so it runs in a thread and
  never in the event loop; the tick engine and the pages go on meanwhile. So does the question
  whether an update runs (it asks systemd, up to 5 s: v0.3.8 p96). Stopping the loop (shutdown)
  stops the git command at once instead of waiting for it (`runner.in_thread`, p95).
- A check that fails shows nothing (no message, no WARNING): it is logged at INFO and tried
  again. An earlier good result stays: a network error must not hide a notice that holds.
- An installation without git (a development run) is never asked; the loop ends.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from reprintbuddy.ops.runner import in_thread
from reprintbuddy.ops.versions import VersionInfo

log = logging.getLogger(__name__)

# p166: this line reaches the system log. git names the remote address in its error; a user and a
# token in that address (`https://user:token@host/…`) are replaced before the line is written.
_CREDENTIALS_IN_URL = re.compile(r"(?<=://)[^/@\s]+@")


def _without_credentials(text: str) -> str:
    return _CREDENTIALS_IN_URL.sub("***@", text)

# Spec 11.3a: "once a day". The spec says "next time" for a failed check and fixes no distance;
# an hour, so that a start without network (a container that boots before the router) does not
# leave the notice silent for a whole day.
CHECK_INTERVAL = 24 * 3600  # seconds between two successful checks
RETRY_AFTER_FAILURE = 3600  # seconds until the next try after a failed check
# p105 (v0.3.7): a round skipped because an update runs is tried again in seconds, like the tick
# engine does (`engine.UPDATE_RETRY_SECONDS`, p41). Root: the first check after a restart falls into
# the update unit that is still running; it had waited an hour (the failure pause). Rejected: waiting
# on the update unit itself (the check only knows `busy()`). Tests: test_autocheck.py "p105".
UPDATE_RETRY = 5  # seconds until the next try after a round skipped for a running update


class AutoCheck:
    """`run()` checks at once, then after each pause the previous round asked for.

    `enabled()` reads the setting afresh each round; `is_install()` says whether the installation
    can be checked at all; `busy()` whether an update runs (its files are in motion, so the round
    is skipped and tried again in `UPDATE_RETRY` seconds); `check()` is the blocking check;
    `store(info)` keeps a good result; `wait(delay)` pauses (the app lets the settings switch end it early).
    """

    def __init__(
        self,
        *,
        enabled: Callable[[], bool],
        is_install: Callable[[], bool],
        busy: Callable[[], bool],
        check: Callable[[], VersionInfo],
        store: Callable[[VersionInfo], None],
        wait: Callable[[float], Awaitable[None]],
    ) -> None:
        self._enabled = enabled
        self._is_install = is_install
        self._busy = busy
        self._check = check
        self._store = store
        self._wait = wait

    async def run_once(self) -> float | None:
        """One round; the pause to take afterwards, or None when there is nothing to ask ever."""
        if not self._is_install():
            return None
        if not self._enabled():
            return CHECK_INTERVAL  # the settings switch wakes the pause
        if await in_thread(self._busy):
            return UPDATE_RETRY
        try:
            info = await in_thread(self._check)
        except Exception as exc:  # the remote, the network, git: any failure is silent
            log.info("automatic check for new versions failed: %s", _without_credentials(str(exc))[:300])
            return RETRY_AFTER_FAILURE
        self._store(info)
        return CHECK_INTERVAL

    async def run(self) -> None:
        """Round after round until cancelled, or until `run_once` says there is nothing to ask."""
        while True:
            delay = await self.run_once()
            if delay is None:
                return
            await self._wait(delay)
