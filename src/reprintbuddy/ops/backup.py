"""Database backups with SQLite's online backup API.

In WAL mode the database file alone is not a consistent copy (recent commits may
still sit in the `-wal` file), so a plain file copy is wrong. The backup API reads
a consistent snapshot while the service keeps running, and in the other direction
it writes into a database that is still open.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

BACKUP_DIRNAME = "backups"
KEEP_BACKUPS = 5

_NAME = re.compile(r"^reprintbuddy-.+-(\d{8}T\d{6}Z)\.db$")


def backup_dir_for(db_path: Path) -> Path:
    return db_path.parent / BACKUP_DIRNAME


def connect_existing(path: Path, *, timeout: float = 30) -> sqlite3.Connection:
    """Open a database that must already exist.

    `sqlite3.connect` quietly creates an empty database for a missing file; copied over the
    running one, that would end in a first-run setup. `mode=rw` fails instead.
    """
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=rw", uri=True, timeout=timeout)


def _copy(source: Path, target: Path, *, target_exists: bool = False) -> None:
    """Copy the content of one SQLite database into another, both closed afterwards."""
    src = connect_existing(source)
    try:
        dst = connect_existing(target) if target_exists else sqlite3.connect(str(target), timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def backup_database(db_path: Path, backup_dir: Path, *, label: str, now: datetime) -> Path:
    """Write a backup named `reprintbuddy-<label>-<UTC time>.db` and return its path."""
    if not db_path.is_file():
        # sqlite3.connect would quietly create an empty database.
        raise FileNotFoundError(str(db_path))
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", label)
    backup_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        backup_dir.chmod(0o700)
    target = backup_dir / f"reprintbuddy-{safe}-{now:%Y%m%dT%H%M%SZ}.db"
    try:
        _copy(db_path, target)
        if os.name == "posix":
            target.chmod(0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink()  # never leave half a backup that looks like a good one
        raise
    return target


def restore_database(backup: Path, db_path: Path) -> None:
    """Replace the content of the database by the backup; safe while the service has it open."""
    if not backup.is_file():
        raise FileNotFoundError(str(backup))
    _copy(backup, db_path, target_exists=True)


def prune_backups(
    backup_dir: Path, keep: int = KEEP_BACKUPS, *, protect: Iterable[Path] = ()
) -> list[Path]:
    """Keep the newest `keep` backups (by the time in the name); return the deleted ones.

    A `protect`ed file is never deleted (a failed rollback names it as the one to restore).
    """
    protected = {Path(path).name for path in protect}
    found: list[tuple[str, Path]] = []
    if backup_dir.is_dir():
        for path in backup_dir.iterdir():
            match = _NAME.match(path.name)
            if match and path.is_file():
                found.append((match.group(1), path))
    found.sort(key=lambda item: (item[0], item[1].name))
    doomed = [path for _, path in found[: max(0, len(found) - keep)] if path.name not in protected]
    for path in doomed:
        with contextlib.suppress(OSError):
            path.unlink()
    return doomed
