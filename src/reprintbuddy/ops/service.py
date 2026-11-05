"""Commands for the systemd units, in the exact form the sudoers rule allows."""

from __future__ import annotations

from pathlib import Path

from reprintbuddy.ops.runner import CommandResult, CommandRunner

INSTALL_DIR = Path("/opt/reprintbuddy")
SERVICE = "reprintbuddy.service"
UPDATE_SERVICE = "reprintbuddy-update.service"
SYSTEMCTL = "/usr/bin/systemctl"

# These three command lines are the only ones install/reprintbuddy.sudoers allows.
RESTART_CMD = ("sudo", "-n", SYSTEMCTL, "restart", "--no-block", SERVICE)
RESTART_WAIT_CMD = ("sudo", "-n", SYSTEMCTL, "restart", SERVICE)
START_UPDATE_CMD = ("sudo", "-n", SYSTEMCTL, "start", "--no-block", UPDATE_SERVICE)


def request_restart(runner: CommandRunner) -> CommandResult:
    """Ask systemd to restart the service and return at once (the caller is the service)."""
    return runner(RESTART_CMD, timeout=15.0)


def start_update_unit(runner: CommandRunner) -> CommandResult:
    """Start the update helper as its own unit; it survives the restart of the service."""
    return runner(START_UPDATE_CMD, timeout=15.0)


def update_unit_state(runner: CommandRunner) -> str:
    """systemd's ActiveState of the update unit ("active", "activating", "inactive", "failed" …).

    Read-only, needs no sudo. Empty when systemd cannot be asked.
    """
    result = runner((SYSTEMCTL, "show", "--property=ActiveState", "--value", UPDATE_SERVICE), timeout=5.0)
    return result.stdout.strip() if result.ok else ""
