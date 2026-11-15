"""Commands for the systemd units, in the exact form the sudoers rule allows."""

from __future__ import annotations

from pathlib import Path

from reprintbuddy.ops.runner import CommandResult, CommandRunner

INSTALL_DIR = Path("/opt/reprintbuddy")
SERVICE = "reprintbuddy.service"
UPDATE_SERVICE = "reprintbuddy-update.service"
SYSTEMCTL = "/usr/bin/systemctl"

# These command lines are the only ones install/reprintbuddy.sudoers allows.
RESTART_CMD = ("sudo", "-n", SYSTEMCTL, "restart", "--no-block", SERVICE)
RESTART_WAIT_CMD = ("sudo", "-n", SYSTEMCTL, "restart", SERVICE)
START_UPDATE_CMD = ("sudo", "-n", SYSTEMCTL, "start", "--no-block", UPDATE_SERVICE)
# A rollback restores the database while the service is stopped.
STOP_CMD = ("sudo", "-n", SYSTEMCTL, "stop", SERVICE)
START_CMD = ("sudo", "-n", SYSTEMCTL, "start", SERVICE)
# Read-only, needs no sudo. "stopping" while the system shuts down (systemctl(1), "is-system-running", table
# "Manager Operation States": "stopping: The manager is shutting down.").
SYSTEM_STATE_CMD = (SYSTEMCTL, "is-system-running")


def request_restart(runner: CommandRunner) -> CommandResult:
    """Ask systemd to restart the service and return at once (the caller is the service)."""
    return runner(RESTART_CMD, timeout=15.0)


def start_update_unit(runner: CommandRunner) -> CommandResult:
    """Start the update helper as its own unit; it survives the restart of the service."""
    return runner(START_UPDATE_CMD, timeout=15.0)


def shutting_down(runner: CommandRunner) -> bool:
    """Whether the system is shutting down (p610): then a service started now starts again only with the next boot."""
    return runner(SYSTEM_STATE_CMD, timeout=5.0).stdout.strip() == "stopping"


def update_unit_state(runner: CommandRunner) -> str:
    """systemd's ActiveState of the update unit ("active", "activating", "inactive", "failed" …).

    Read-only, needs no sudo. Empty when systemd cannot be asked.
    """
    result = runner((SYSTEMCTL, "show", "--property=ActiveState", "--value", UPDATE_SERVICE), timeout=5.0)
    return result.stdout.strip() if result.ok else ""
