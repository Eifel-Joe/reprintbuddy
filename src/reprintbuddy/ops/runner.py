"""Run external commands; the one place that starts processes."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


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

    A missing program gives 127 and a timeout 124 (the usual shell conventions).
    `GIT_TERMINAL_PROMPT=0`: git must fail instead of waiting for a password nobody can type.
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        done = subprocess.run(
            list(args),
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        return CommandResult(127, "", str(error))
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"timed out after {timeout:g} s")
    return CommandResult(done.returncode, done.stdout or "", done.stderr or "")
