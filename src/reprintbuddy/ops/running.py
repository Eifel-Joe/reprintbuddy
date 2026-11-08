"""Is an update running right now?

One answer for everybody who needs it: the write guard of the web interface, and later
the background poll, which pauses while an update is in progress. The state file alone
is not enough: after a container restart, or a unit that failed before it wrote anything,
it would say "running" for half an hour. systemd tells the truth.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from reprintbuddy.ops.runner import CommandRunner, in_thread
from reprintbuddy.ops.service import update_unit_state
from reprintbuddy.ops.status import BUSY_STATES, is_busy, read_status

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


async def update_running_async(data_dir: Path, runner: CommandRunner, now: datetime) -> bool:
    """`update_running` for code that runs in the event loop (v0.3.8 p96).

    While an update runs it asks systemd, which may take seconds; that happens in a worker thread,
    so the pages and the tick engine are not held. Stopping the awaiting task kills the question.
    """
    return await in_thread(update_running, data_dir, runner, now)


def update_just_started(data_dir: Path, now: datetime) -> bool:
    """Was an update (or its start) written to the state file within the last `QUEUE_GRACE`?

    Cheap and without systemd: one small file. The engine asks it synchronously right before a print
    is marked as sent, after the slower question `update_running_async` was answered in a worker
    thread: the update's start (`update_start` writes "queued" without an `await`) can fall between the
    two answers, and its first step is the backup of the database (v0.3.8 p96 review).
    """
    status = read_status(data_dir)
    return (
        status.state in BUSY_STATES and status.started_at is not None and now - status.started_at < QUEUE_GRACE
    )
