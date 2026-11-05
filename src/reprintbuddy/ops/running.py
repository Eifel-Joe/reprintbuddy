"""Is an update running right now?

One answer for everybody who needs it: the write guard of the web interface, and later
the background poll, which pauses while an update is in progress. The state file alone
is not enough: after a container restart, or a unit that failed before it wrote anything,
it would say "running" for half an hour. systemd tells the truth.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from reprintbuddy.ops.runner import CommandRunner
from reprintbuddy.ops.service import update_unit_state
from reprintbuddy.ops.status import is_busy, read_status

# systemd says the unit is not running (anything else, or no answer, counts as running).
DEAD_UNIT_STATES = frozenset({"inactive", "failed"})
# Between `start --no-block` and the unit showing as active.
QUEUE_GRACE = timedelta(seconds=15)


def update_running(data_dir: Path, runner: CommandRunner, now: datetime) -> bool:
    """True while an update is really in progress (not merely recorded as in progress)."""
    status = read_status(data_dir)
    if not is_busy(status, now):
        return False
    if (
        status.state == "queued"
        and status.started_at is not None
        and now - status.started_at < QUEUE_GRACE
    ):
        return True
    # Unknown (systemd silent or missing) counts as running: do not guess an update away.
    return update_unit_state(runner) not in DEAD_UNIT_STATES
