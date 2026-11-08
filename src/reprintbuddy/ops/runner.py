"""Run external commands; the one place that starts processes."""

from __future__ import annotations

import asyncio
import contextvars
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

CANCELLED = 130  # the usual shell code for "interrupted"; `CommandResult.stderr` says "cancelled"
POLL_SECONDS = 0.1  # how often a running command looks at the cancel flag and the clock

# The cancel flag of the commands started from the current thread (see `in_thread`). A context
# variable, not an argument: `asyncio.to_thread` hands the context on to its worker thread, so the
# runners (and test stand-ins) keep their signature.
cancel_flag: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "reprintbuddy_cancel_flag", default=None
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner(Protocol):
    def __call__(
        self, args: Sequence[str], *, cwd: Path | None = None, timeout: float = 120.0
    ) -> CommandResult: ...


def subprocess_runner(
    args: Sequence[str], *, cwd: Path | None = None, timeout: float = 120.0
) -> CommandResult:
    """Run `args` and return the outcome; never raises because of the exit code.

    A missing program gives 127, a timeout 124 (the usual shell conventions) and a command stopped
    by its cancel flag (`in_thread`) 130 with the process killed. `GIT_TERMINAL_PROMPT=0`: git must
    fail instead of waiting for a password nobody can type.

    Root of the cancel flag (v0.3.8 p95): `subprocess.run` cannot be stopped from outside, so a check
    for new versions (`git ls-remote`, up to 20 s) made the shutdown wait for it. The process is
    waited for in short steps that look at the flag.
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        process = subprocess.Popen(
            list(args),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as error:
        return CommandResult(127, "", str(error))
    stop = cancel_flag.get()
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=POLL_SECONDS)
                return CommandResult(process.returncode, stdout or "", stderr or "")
            except subprocess.TimeoutExpired:
                if stop is not None and stop.is_set():
                    code, why = CANCELLED, "cancelled"
                elif time.monotonic() >= deadline:
                    code, why = 124, f"timed out after {timeout:g} s"
                else:
                    continue
                process.kill()
                # As `subprocess.run` does: on POSIX only wait (a child of the command may hold the
                # pipes open and `communicate` would wait for that one too).
                if os.name == "nt":
                    process.communicate()
                else:
                    process.wait()
                return CommandResult(code, "", why)
    except BaseException:
        process.kill()
        raise


async def in_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run blocking `func` in a worker thread; stopping the awaiting task stops its commands too.

    If the task is cancelled (shutdown, a closed request), the commands `func` started through
    `subprocess_runner` are killed within `POLL_SECONDS` and this returns at once, instead of the
    worker thread running a git command to its time limit while the process is closing.
    """
    stop = threading.Event()
    token = cancel_flag.set(stop)
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except asyncio.CancelledError:
        stop.set()
        raise
    finally:
        cancel_flag.reset(token)
