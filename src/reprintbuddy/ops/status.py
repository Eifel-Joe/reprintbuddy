"""The state of an update, in a file next to the database.

Not in the database: a rollback restores the database from the backup made before
the update and would wipe the very record of what happened.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Mapping, get_args

STATUS_FILENAME = "update-status.json"

UpdateState = Literal["idle", "queued", "running", "succeeded", "failed", "rolled_back", "rollback_failed"]
UPDATE_STEPS = (
    "start", "backup", "fetch", "checkout", "install", "verify", "migrate",
    "restart", "health", "rollback", "done",
)
BUSY_STATES = frozenset({"queued", "running"})
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

        return cls(
            state=state,  # type: ignore[arg-type]
            step=str(data.get("step") or ""),
            from_tag=text("from_tag"),
            to_tag=text("to_tag"),
            started_at=moment("started_at"),
            finished_at=moment("finished_at"),
            message=str(data.get("message") or ""),
            backup=text("backup"),
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
