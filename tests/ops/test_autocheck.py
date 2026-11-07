"""The automatic check for new versions (v0.3.6, Spec 11.3a), the loop without web and without time.

`check` stands for `check_versions` (git and the network), `wait` for the pause between two
rounds. Nothing here asks a remote or sleeps.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime

import pytest

from reprintbuddy.ops import autocheck
from reprintbuddy.ops.autocheck import CHECK_INTERVAL, RETRY_AFTER_FAILURE, AutoCheck
from reprintbuddy.ops.versions import UpdateCheckError, VersionInfo

INFO = VersionInfo("v0.1.0", "v0.2.0", datetime(2026, 11, 7, tzinfo=UTC))


class Setup:
    def __init__(self, **over):
        self.enabled = True
        self.install = True
        self.busy = False
        self.checks = 0
        self.stored: list[VersionInfo] = []
        self.outcome = INFO  # a VersionInfo, or an exception to raise
        self.waits: list[float] = []
        self.stop_after = 99
        self.__dict__.update(over)

    def check(self) -> VersionInfo:
        self.checks += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def wait(self, delay: float) -> None:
        self.waits.append(delay)
        if len(self.waits) >= self.stop_after:
            raise asyncio.CancelledError

    def build(self) -> AutoCheck:
        return AutoCheck(
            enabled=lambda: self.enabled,
            is_install=lambda: self.install,
            busy=lambda: self.busy,
            check=self.check,
            store=self.stored.append,
            wait=self.wait,
        )


def test_the_intervals_are_a_day_and_an_hour():
    # Spec 11.3a: once a day; "next time" after a failure is an hour (a start without network
    # must not leave the page silent for a whole day).
    assert CHECK_INTERVAL == 24 * 3600
    assert RETRY_AFTER_FAILURE == 3600


async def test_a_good_check_is_stored_and_the_next_one_is_a_day_away():
    s = Setup()
    assert await s.build().run_once() == CHECK_INTERVAL
    assert s.stored == [INFO]


async def test_the_loop_checks_at_the_start_and_then_every_day():
    s = Setup(stop_after=3)
    with pytest.raises(asyncio.CancelledError):
        await s.build().run()
    assert s.checks == 3  # at once, then after each wait
    assert s.waits == [CHECK_INTERVAL] * 3


@pytest.mark.parametrize(
    "failure",
    [UpdateCheckError("fatal: unable to access"), TimeoutError(), OSError("no route"), RuntimeError("odd")],
    ids=["check-error", "timeout", "os-error", "anything-else"],
)
async def test_a_failed_check_stores_nothing_and_is_retried_in_an_hour(failure, caplog):
    s = Setup(outcome=failure)
    with caplog.at_level(logging.DEBUG, logger=autocheck.__name__):
        assert await s.build().run_once() == RETRY_AFTER_FAILURE
    assert s.stored == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []  # nothing loud


async def test_after_a_failure_the_next_good_check_is_stored():
    s = Setup(outcome=UpdateCheckError("down"))
    check = s.build()
    await check.run_once()
    s.outcome = INFO
    assert await check.run_once() == CHECK_INTERVAL
    assert s.stored == [INFO]


async def test_switched_off_checks_nothing():
    s = Setup(enabled=False)
    assert await s.build().run_once() == CHECK_INTERVAL
    assert s.checks == 0 and s.stored == []


async def test_without_a_git_installation_the_loop_ends_without_asking():
    s = Setup(install=False)
    await s.build().run()  # returns
    assert s.checks == 0 and s.waits == []


async def test_while_an_update_runs_the_round_is_skipped():
    s = Setup(busy=True)
    assert await s.build().run_once() == RETRY_AFTER_FAILURE
    assert s.checks == 0


async def test_the_check_runs_in_a_thread_and_the_loop_stays_free():
    release = threading.Event()
    seen: dict[str, object] = {}
    main = threading.current_thread()

    def slow_check() -> VersionInfo:
        seen["thread"] = threading.current_thread()
        assert release.wait(5)
        return INFO

    stored: list[VersionInfo] = []
    check = AutoCheck(
        enabled=lambda: True,
        is_install=lambda: True,
        busy=lambda: False,
        check=slow_check,
        store=stored.append,
        wait=lambda delay: asyncio.sleep(0),
    )
    task = asyncio.create_task(check.run_once())
    for _ in range(500):  # the event loop keeps turning while git "runs"
        await asyncio.sleep(0.001)
        if "thread" in seen:
            break
    assert "thread" in seen and not task.done()
    await asyncio.sleep(0.01)  # and still turns, with the check blocked
    assert not task.done()
    release.set()
    assert await asyncio.wait_for(task, 5) == CHECK_INTERVAL
    assert seen["thread"] is not main
    assert stored == [INFO]
