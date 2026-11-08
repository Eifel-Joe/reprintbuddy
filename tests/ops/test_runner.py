import sys

from reprintbuddy.ops.runner import CommandResult, subprocess_runner


def test_runner_success():
    result = subprocess_runner([sys.executable, "-c", "print('hi')"])
    assert result.returncode == 0
    assert result.stdout.replace("\r\n", "\n") == "hi\n"
    assert result.ok


def test_runner_nonzero():
    result = subprocess_runner([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert result.returncode == 3
    assert not result.ok


def test_runner_missing_program():
    assert subprocess_runner(["definitely-not-a-program-xyz"]).returncode == 127


def test_runner_timeout():
    result = subprocess_runner(
        [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5
    )
    assert result.returncode == 124


def test_runner_disables_git_prompt():
    code = "import os; print(os.environ['GIT_TERMINAL_PROMPT'])"
    result = subprocess_runner([sys.executable, "-c", code])
    assert result.stdout.strip() == "0"


def test_command_result_is_frozen():
    import dataclasses

    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        CommandResult(0).returncode = 1  # type: ignore[misc]


# --- stopping a command from outside (v0.3.8 p95) ---------------------------------------------------------

import asyncio  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

from reprintbuddy.ops import runner as runner_module  # noqa: E402
from reprintbuddy.ops.runner import CANCELLED, cancel_flag, in_thread  # noqa: E402

SLEEP_30 = [sys.executable, "-c", "import time; time.sleep(30)"]


def test_a_command_with_a_set_cancel_flag_is_killed_and_says_so():
    flag = threading.Event()
    token = cancel_flag.set(flag)
    try:
        threading.Timer(0.3, flag.set).start()
        started = time.monotonic()
        result = subprocess_runner(SLEEP_30, timeout=60)
    finally:
        cancel_flag.reset(token)
    assert result.returncode == CANCELLED == 130
    assert result.stderr == "cancelled"
    assert time.monotonic() - started < 5


def test_without_a_flag_nothing_changes_for_a_slow_command():
    result = subprocess_runner([sys.executable, "-c", "import time; time.sleep(0.4); print('late')"])
    assert result.ok and result.stdout.strip() == "late"


def test_a_big_output_is_read_completely():
    result = subprocess_runner([sys.executable, "-c", "print('x' * 3_000_000)"])
    assert result.ok and len(result.stdout.strip()) == 3_000_000


async def test_cancelling_the_task_kills_the_command_and_returns_at_once():
    finished = threading.Event()
    seen: list = []

    def work():
        seen.append(subprocess_runner(SLEEP_30, timeout=60))
        finished.set()

    task = asyncio.create_task(in_thread(work))
    await asyncio.sleep(0.4)  # the command is running
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 1  # the awaiting task does not wait for the command
    assert await asyncio.to_thread(finished.wait, 5)  # and the command is gone soon after
    assert seen[0].returncode == CANCELLED


async def test_in_thread_returns_the_result_and_passes_errors_on():
    assert await in_thread(lambda a, b=0: a + b, 1, b=2) == 3
    with pytest.raises(ZeroDivisionError):
        await in_thread(lambda: 1 / 0)


async def test_the_flag_is_not_left_set_for_the_next_call():
    await in_thread(lambda: None)
    assert cancel_flag.get() is None
    result = await in_thread(lambda: subprocess_runner([sys.executable, "-c", "print('ok')"]))
    assert result.ok


def test_the_poll_step_is_short():
    assert runner_module.POLL_SECONDS <= 0.25
