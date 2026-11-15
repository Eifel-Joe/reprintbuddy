"""The end of a restore from a full backup, the same for the page and the command line (Spec 11.6, p531).

A full backup can be restored on the settings page (`web/routes/backup.py`) and with the emergency command
`reprintbuddy restore-backup` (`ops/restorecmd.py`) when the page cannot be reached. Both check the file with
the same functions of `fullbackup`; what differs around them is on purpose (the page: a running service, a
browser that may go away, a restart; the command line: a stopped service, a terminal, a database that may be
broken). What is the same comes from here, so that a fix reaches both ways at once.
"""

from __future__ import annotations

from pathlib import Path

from reprintbuddy.ops.status import read_status


def protected_backups(data_dir: Path) -> list[Path]:
    """The backups the pruning after a restore must keep beyond the newest ones: the one a failed rollback
    of an update names as the one to restore (as the update helper keeps it, `ops/updater.py`)."""
    status = read_status(data_dir)
    return [Path(status.backup)] if status.state == "rollback_failed" and status.backup else []
