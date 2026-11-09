"""States the activity log records only as changes (Spec 10.7): the result of the last update, the
connection to Bambuddy and its rights. Each function reads what exists and names the state; the
writer compares it with the last one logged."""

from __future__ import annotations

from typing import Any

from reprintbuddy.ops.status import UpdateStatus

# The final states of an update and their fixed result (Spec 10.7, group 4).
_RESULTS = {
    "succeeded": "done",
    "failed": "failed",
    "rolled_back": "rolled_back",
    "rollback_failed": "rollback_failed",
}
# What `ops.status.mark_interrupted` writes into the message of an update stopped from outside.
INTERRUPTED = ": interrupted"


def update_outcome(status: UpdateStatus) -> dict[str, Any] | None:
    """The result of a finished update with its start as identity, or None (nothing finished).

    Only fixed words: the message of git or pip never reaches the log.
    """
    result = _RESULTS.get(status.state)
    if result is None or status.started_at is None:
        return None
    if INTERRUPTED in status.message:
        result = "interrupted"
    return {
        "started_at": status.started_at.isoformat(),
        "finished_at": status.finished_at.isoformat() if status.finished_at else None,
        "from": status.from_tag,
        "to": status.to_tag,
        "result": result,
    }


def connection_state(engine_status: Any) -> str | None:
    """After a regular poll: "ok", "unreachable" or "refused"; None when nothing is set up."""
    if not engine_status.configured:
        return None
    if engine_status.reachable is False:
        return "unreachable"
    if engine_status.reachable is None:
        return None
    # Only the answer of the regular poll: a refused hand-over of a print is the journal's.
    if engine_status.poll_error is not None:
        return "refused"
    return "ok"


def rights_state(monitor: Any) -> str | None:
    """"ok" or "missing" once the check of the stored key has a verdict (Spec 11.3); else None."""
    return monitor.status if monitor.status in ("ok", "missing") else None
