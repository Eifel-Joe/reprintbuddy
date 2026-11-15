"""The end of a restore from a full backup, the same for the page and the command line (Spec 11.6, p531).

A full backup can be restored on the settings page (`web/routes/backup.py`) and with the emergency command
`reprintbuddy restore-backup` (`ops/restorecmd.py`) when the page cannot be reached. Both check the file with
the same functions of `fullbackup`; what differs around them is on purpose (the page: a running service, a
browser that may go away, a restart; the command line: a stopped service, a terminal, a database that may be
broken). What is the same comes from here, so that a fix reaches both ways at once.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from reprintbuddy.activity.writer import current_marks, current_notify_marks
from reprintbuddy.ops import fullbackup as fb
from reprintbuddy.ops.backup import _copy, backup_dir_for, connect_existing
from reprintbuddy.ops.status import read_status


@dataclass(frozen=True)
class Installed:
    """What `install` did: where the database that was there went, and what the next start needs from it."""

    before: Path | None  # its backup (`before-restore`), the file set aside (`damaged-`), or None: there was none
    marks: dict[str, dict[str, str | None]] | None  # p186: the log's marks of the replaced database
    notify_marks: list[dict[str, Any]] | None  # p220: the push messages' marks of the replaced database


def install(
    candidate: Path,
    db_path: Path,
    *,
    now: datetime,
    protect: Iterable[Path] = (),
    may_set_aside: bool = False,
) -> Installed:
    """Put the checked candidate in place of the database: the one step of a restore that changes it.

    The marks of the log and of the push messages are read from the database that is replaced (they belong
    to what has happened, not to the data); `fb.replace_database` backs it up and copies the candidate in,
    in one step. Raises when nothing was replaced; once it returns, the database is the restored one.

    `may_set_aside` is for the command line only, when the service is stopped: a database that is missing
    or cannot be read is set aside (`set_aside_and_install`) instead of backed up. The page never passes
    it. Its service holds the database open, and a check that fails for a moment (locked, say) must
    never move the live database away; without it the page always backs up and copies in (as before p531).
    Test: tests/ops/test_restore_shared.py::test_without_leave_a_database_that_seems_unreadable_is_never_set_aside
    """
    if may_set_aside and not database_readable(db_path):
        return Installed(set_aside_and_install(candidate, db_path, now), None, None)
    marks = current_marks(db_path)
    notify_marks = current_notify_marks(db_path)
    before = fb.replace_database(candidate, db_path, now=now, protect=protect)
    return Installed(before, marks, notify_marks)


def protected_backups(data_dir: Path) -> list[Path]:
    """The backups the pruning after a restore must keep beyond the newest ones: the one a failed rollback
    of an update names as the one to restore (as the update helper keeps it, `ops/updater.py`)."""
    status = read_status(data_dir)
    return [Path(status.backup)] if status.state == "rollback_failed" and status.backup else []


# --- the command line only: a database that is missing or cannot be read -------------------------------


def database_readable(db_path: Path) -> bool:
    """Can SQLite read the database in place? A garbage file answers `PRAGMA page_size` all the same
    (it needs no page), so the schema is read and a quick integrity check is made."""
    if not db_path.is_file():
        return False
    try:
        connection = connect_existing(db_path, timeout=5)
        try:
            connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            return connection.execute("PRAGMA quick_check(1)").fetchone() == ("ok",)
        finally:
            connection.close()
    except sqlite3.Error:
        return False


class RestoreNotUndone(Exception):
    """Putting the new database in failed AND the old file could not be moved back: it lies at `where`."""

    def __init__(self, where: Path) -> None:
        super().__init__(str(where))
        self.where = where


def _private_folder(folder: Path) -> None:
    if os.name == "posix":
        folder.chmod(0o700)


def _private_file(path: Path) -> None:
    if os.name == "posix" and path.exists():
        path.chmod(0o600)


def set_aside_and_install(candidate: Path, db_path: Path, now: datetime) -> Path | None:
    """The database in place is missing or cannot be read: set it aside, put the candidate there.

    Returns where the old file went, None when there was none. The old file is moved, not copied
    (a broken file may not be copyable by SQLite) and its name matches no pattern of the pruning.
    If putting the new one in fails, the old file is moved back: a refusal changes nothing. If even
    that fails, `RestoreNotUndone` says where the old file is.
    """
    suffixes = ("-wal", "-shm", "-journal")
    aside: Path | None = None
    moved: list[tuple[Path, Path]] = []  # (where it was, where it is now), the main file first
    installing = False
    try:
        if db_path.exists():
            folder = backup_dir_for(db_path)
            folder.mkdir(parents=True, exist_ok=True)
            _private_folder(folder)
            aside = folder / f"damaged-{now:%Y%m%dT%H%M%SZ}.db"
            db_path.replace(aside)
            moved.append((db_path, aside))
            for suffix in suffixes:  # they belong to the file that was moved away
                leftover = Path(f"{db_path}{suffix}")
                if leftover.exists():
                    leftover.replace(Path(f"{aside}{suffix}"))
                    moved.append((leftover, Path(f"{aside}{suffix}")))
        installing = True
        fb._prepare(candidate, fb._page_size(candidate))
        _copy(candidate, db_path)
        _private_file(db_path)
    except BaseException:
        if installing:  # the files at the place are the new ones; before, they may still be the old ones
            fb.remove_database_files(db_path)
        try:
            for origin, where in reversed(moved):
                where.replace(origin)
        except OSError as error:
            raise RestoreNotUndone(aside or db_path) from error
        raise
    fb.remove_database_files(candidate)
    return aside
