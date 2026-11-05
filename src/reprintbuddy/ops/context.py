"""What the web layer needs from the operating system, handed in once at start."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reprintbuddy.ops.runner import CommandRunner, subprocess_runner
from reprintbuddy.ops.server import EffectiveServer
from reprintbuddy.ops.service import INSTALL_DIR


@dataclass(frozen=True)
class OpsContext:
    install_dir: Path = INSTALL_DIR
    runner: CommandRunner = subprocess_runner
    effective: EffectiveServer | None = None  # what this process was started with
