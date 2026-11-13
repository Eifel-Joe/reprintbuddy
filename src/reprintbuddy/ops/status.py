"""The state of an update, in a file next to the database.

Not in the database: a rollback restores the database from the backup made before
the update and would wipe the very record of what happened.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Mapping, get_args

STATUS_FILENAME = "update-status.json"

UpdateState = Literal["idle", "queued", "running", "succeeded", "failed", "rolled_back", "rollback_failed"]
UPDATE_STEPS = (
    "start", "backup", "fetch", "checkout", "install", "verify", "migrate",
    "restart", "health", "rollback", "done",
)
# v0.3.14 p176: what went wrong, as a code the page turns into text in the language of the viewer
# (`ops.update.reason.<code>`). `message` stays as it was: the English line for the log and for old
# readers. Parameters: `detail` is a technical line of git, pip or the system and stays as it is.
UPDATE_REASONS = (
    "start_failed", "head_unreadable", "backup_failed", "fetch_failed", "no_newer", "unknown_target",
    "checkout_failed", "snapshot_failed", "install_failed", "python_too_old", "verify_failed",
    "migrate_failed", "restart_failed", "health_failed", "unexpected",
    "interrupted_untouched", "interrupted_partial",
)
BUSY_STATES = frozenset({"queued", "running"})
# The results that count as a failure: their card stays until the user hides it (p503, Spec 11.3a).
FAILED_STATES = frozenset({"failed", "rolled_back", "rollback_failed"})
# An update older than this is taken for stuck (for example the container was restarted midway).
STALE_AFTER = timedelta(minutes=30)


@dataclass(frozen=True)
class UpdateStatus:
    state: UpdateState = "idle"
    step: str = ""
    from_tag: str | None = None
    to_tag: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str = ""
    backup: str | None = None
    reason: str = ""
    params: dict[str, str] = field(default_factory=dict)
    # p503 (Spec 11.3a): the card of this result was seen (success) or hidden by the user (failure).
    # The record itself stays; only the card goes. Each new update writes a fresh state without it.
    acknowledged: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "state": self.state,
            "step": self.step,
            "from_tag": self.from_tag,
            "to_tag": self.to_tag,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "message": self.message,
            "backup": self.backup,
            "reason": self.reason,
            "params": dict(self.params),
            "acknowledged": self.acknowledged,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> "UpdateStatus":
        state = data.get("state", "idle")
        if state not in get_args(UpdateState):
            raise ValueError("unknown state")

        def moment(key: str) -> datetime | None:
            value = data.get(key)
            return datetime.fromisoformat(str(value)) if value else None

        def text(key: str) -> str | None:
            value = data.get(key)
            return None if value is None else str(value)

        reason = str(data.get("reason") or "")
        raw_params = data.get("params")
        params = (
            {str(key): str(value) for key, value in raw_params.items()} if isinstance(raw_params, dict) else {}
        )
        return cls(
            state=state,  # type: ignore[arg-type]
            step=str(data.get("step") or ""),
            from_tag=text("from_tag"),
            to_tag=text("to_tag"),
            started_at=moment("started_at"),
            finished_at=moment("finished_at"),
            message=str(data.get("message") or ""),
            backup=text("backup"),
            reason=reason if reason in UPDATE_REASONS else "",
            params=params,
            acknowledged=data.get("acknowledged") is True,
        )


def status_path(data_dir: Path) -> Path:
    return data_dir / STATUS_FILENAME


def read_status(data_dir: Path) -> UpdateStatus:
    """The stored state; a missing or unreadable file means "no update has run"."""
    try:
        data = json.loads(status_path(data_dir).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return UpdateStatus()
        return UpdateStatus.from_json(data)
    except (OSError, ValueError, TypeError):
        return UpdateStatus()


def write_status(data_dir: Path, status: UpdateStatus) -> None:
    """Write atomically: a reader sees the old or the new state, never half of one."""
    path = status_path(data_dir)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(status.to_json(), indent=2), encoding="utf-8")
    if os.name == "posix":
        temp.chmod(0o600)
    os.replace(temp, path)


def is_busy(status: UpdateStatus, now: datetime) -> bool:
    return (
        status.state in BUSY_STATES
        and status.started_at is not None
        and now - status.started_at < STALE_AFTER
    )


# Steps before the checkout change nothing; from the checkout on the installation may be half switched.
_UNTOUCHED_STEPS = frozenset({"start", "backup", "fetch"})


def mark_interrupted(data_dir: Path, now: datetime) -> UpdateStatus | None:
    """Write a final state for an update that was stopped from outside (systemd timeout).

    Returns the new state, or None when no update was in progress.
    """
    current = read_status(data_dir)
    if current.state not in BUSY_STATES:
        return None
    step = current.step or "start"
    if step in _UNTOUCHED_STEPS:
        state, message = "failed", f"{step}: interrupted, nothing was changed"
        reason = "interrupted_untouched"
    else:
        state = "rollback_failed"
        reason = "interrupted_partial"
        message = f"{step}: interrupted, the installation may be incomplete"
        if current.backup:
            message += f" \u2013 backup: {current.backup}"
    final = UpdateStatus(
        state=state,  # type: ignore[arg-type]
        step=step,
        from_tag=current.from_tag,
        to_tag=current.to_tag,
        started_at=current.started_at,
        finished_at=now,
        message=message,
        backup=current.backup,
        reason=reason,
    )
    write_status(data_dir, final)
    return final
