"""Commands for the systemd units, in the exact form the sudoers rule allows."""

from __future__ import annotations

import signal
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


# p644 (v1.2.1). Root: systemd stops a unit by sending SIGTERM to every process of its control group
# (systemd.kill(5): KillMode defaults to control-group), and the `sudo systemctl restart --no-block` the service
# started for itself belongs to that group. sudo relays the signal and, once its command ended by it, sends
# itself the same signal (sudo(8), "Signal handling"): the runner sees -SIGTERM and nothing on stderr. The
# restart was asked for and is under way; it was logged as "restart after restore failed:" without a reason,
# and the restart button showed "Restart failed".
# Why -SIGTERM means "under way" here: only a stop of this unit or of the whole system sends SIGTERM to the
# group. Our own restart is the usual one; a stop or restart typed by somebody else at the same moment, or a
# shutdown, also ends this process, and systemd brings the enabled service back (restart, next boot) or was
# asked not to (stop) - in no case did the request fail. SIGKILL (stop timeout) and any other code stay failures.
# Rejected: asking systemd afterwards whether the unit is deactivating - that is one more command of the same
# group, killed by the same stop.
# Tests: tests/web/test_restart_under_way.py
def restart_under_way(result: CommandResult) -> bool:
    """Whether a restart the service asked for itself went through (see above)."""
    return result.ok or result.returncode == -int(signal.SIGTERM)


def start_update_unit(runner: CommandRunner) -> CommandResult:
    """Start the update helper as its own unit; it survives the restart of the service."""
    return runner(START_UPDATE_CMD, timeout=15.0)


def shutting_down(runner: CommandRunner) -> bool:
    """Whether the system is shutting down (p610): the enabled service then starts with the next boot at the latest."""
    return runner(SYSTEM_STATE_CMD, timeout=5.0).stdout.strip() == "stopping"


def update_unit_state(runner: CommandRunner) -> str:
    """systemd's ActiveState of the update unit ("active", "activating", "inactive", "failed" …).

    Read-only, needs no sudo. Empty when systemd cannot be asked.
    """
    result = runner((SYSTEMCTL, "show", "--property=ActiveState", "--value", UPDATE_SERVICE), timeout=5.0)
    return result.stdout.strip() if result.ok else ""
